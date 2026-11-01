import re
import secrets
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from common.log import event
from generated import flight_pb2 as pb
from generated import flight_pb2_grpc as rpc

FLIGHT_SELECT = "SELECT f.*, a.name AS airline FROM flights f JOIN airlines a ON a.id=f.airline_id"


class BusinessError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fail(code, message):
    raise BusinessError(code, message)


def uuid(value):
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        fail(grpc.StatusCode.INVALID_ARGUMENT, "Expected a valid UUID")


def timestamp(value):
    result = Timestamp()
    result.FromDatetime(value)
    return result


def flight_message(row):
    return pb.Flight(
        id=str(row["id"]),
        flight_number=row["flight_number"],
        airline=row["airline"],
        origin=row["origin"],
        destination=row["destination"],
        departure_time=timestamp(row["departure_time"]),
        arrival_time=timestamp(row["arrival_time"]),
        total_seats=row["total_seats"],
        available_seats=row["available_seats"],
        price_minor=int(row["price"] * 100),
        status=pb.FlightStatus.Value(row["status"]),
    )


def reservation_message(row):
    return pb.SeatReservation(
        id=str(row["id"]),
        flight_id=str(row["flight_id"]),
        booking_id=str(row["booking_id"]),
        seat_count=row["seat_count"],
        status=pb.ReservationStatus.Value(row["status"]),
        created_at=timestamp(row["created_at"]),
        unit_price_minor=int(row["unit_price"] * 100),
    )


class GuardInterceptor(grpc.ServerInterceptor):
    def __init__(self, api_key):
        self.api_key = api_key

    def intercept_service(self, continuation, details):
        handler = continuation(details)
        if handler is None:
            return None

        def call(request, context):
            keys = [value for key, value in context.invocation_metadata() if key == "x-api-key"]
            if len(keys) != 1 or not secrets.compare_digest(keys[0], self.api_key):
                context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid service credentials")
            try:
                return handler.unary_unary(request, context)
            except BusinessError as error:
                context.abort(error.code, str(error))
            except SQLAlchemyError as error:
                event("flight_database_error", method=details.method, error=type(error).__name__)
                context.abort(
                    grpc.StatusCode.UNAVAILABLE, "Flight database temporarily unavailable"
                )
            except Exception as error:
                event("flight_internal_error", method=details.method, error=type(error).__name__)
                context.abort(grpc.StatusCode.INTERNAL, "Internal flight service error")

        return grpc.unary_unary_rpc_method_handler(
            call,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


class FlightService(rpc.FlightServiceServicer):
    def __init__(self, engine, cache):
        self.engine = engine
        self.cache = cache

    def GetFlight(self, request, context):
        flight_id = uuid(request.id)
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            revision = self.cache.revision(db)
            key = f"flight:{flight_id}:{revision}"
            cached = self.cache.get(key, pb.Flight)
            if cached is not None:
                return cached
            row = (
                db.execute(text(FLIGHT_SELECT + " WHERE f.id=:id"), {"id": flight_id})
                .mappings()
                .first()
            )
            if row is None:
                fail(grpc.StatusCode.NOT_FOUND, "Flight not found")
            result = flight_message(row)
            self.cache.put(key, result)
            return result

    def SearchFlights(self, request, context):
        if not re.fullmatch(r"[A-Z]{3}", request.origin) or not re.fullmatch(
            r"[A-Z]{3}", request.destination
        ):
            fail(grpc.StatusCode.INVALID_ARGUMENT, "Airports must be uppercase IATA codes")
        if request.origin == request.destination:
            fail(grpc.StatusCode.INVALID_ARGUMENT, "Origin and destination must differ")
        params = {"origin": request.origin, "destination": request.destination}
        date_filter = ""
        if request.date:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", request.date):
                    raise ValueError
                day = date.fromisoformat(request.date)
                params["start"] = datetime.combine(day, time.min, UTC)
                params["end"] = params["start"] + timedelta(days=1)
            except (ValueError, OverflowError):
                fail(grpc.StatusCode.INVALID_ARGUMENT, "Date must be YYYY-MM-DD")
            date_filter = " AND f.departure_time >= :start AND f.departure_time < :end"
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            revision = self.cache.revision(db)
            key = (
                f"search:{request.origin}:{request.destination}:{request.date or 'all'}:{revision}"
            )
            cached = self.cache.get(key, pb.SearchFlightsResponse)
            if cached is not None:
                return cached
            rows = db.execute(
                text(
                    FLIGHT_SELECT + " WHERE f.origin=:origin AND f.destination=:destination"
                    " AND f.status='SCHEDULED'" + date_filter + " ORDER BY f.departure_time, f.id"
                ),
                params,
            ).mappings()
            result = pb.SearchFlightsResponse(flights=[flight_message(row) for row in rows])
            self.cache.put(key, result)
            return result

    @staticmethod
    def lock_booking(db, booking_id):
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"), {"id": str(booking_id)}
        )

    def ReserveSeats(self, request, context):
        flight_id, booking_id = uuid(request.flight_id), uuid(request.booking_id)
        if not 1 <= request.seat_count <= 10000:
            fail(grpc.StatusCode.INVALID_ARGUMENT, "seat_count must be between 1 and 10000")
        if request.HasField("expected_price_minor") and request.expected_price_minor <= 0:
            fail(grpc.StatusCode.INVALID_ARGUMENT, "Expected price must be positive")
        with self.engine.begin() as db:
            self.lock_booking(db, booking_id)
            if db.execute(
                text("SELECT 1 FROM reservation_cancellations WHERE booking_id=:id"),
                {"id": booking_id},
            ).first():
                fail(grpc.StatusCode.FAILED_PRECONDITION, "Reservation already released")
            existing = (
                db.execute(
                    text("SELECT * FROM seat_reservations WHERE booking_id=:id"), {"id": booking_id}
                )
                .mappings()
                .first()
            )
            if existing:
                if (
                    existing["flight_id"] != flight_id
                    or existing["seat_count"] != request.seat_count
                ):
                    fail(
                        grpc.StatusCode.ALREADY_EXISTS,
                        "booking_id is already bound to a different reservation",
                    )
                if existing["status"] != "ACTIVE":
                    fail(grpc.StatusCode.FAILED_PRECONDITION, "Reservation is no longer active")
                return reservation_message(existing)
            flight = (
                db.execute(text("SELECT * FROM flights WHERE id=:id FOR UPDATE"), {"id": flight_id})
                .mappings()
                .first()
            )
            if flight is None:
                fail(grpc.StatusCode.NOT_FOUND, "Flight not found")
            if flight["status"] != "SCHEDULED" or flight["departure_time"] <= datetime.now(UTC):
                fail(grpc.StatusCode.FAILED_PRECONDITION, "Flight is not open for reservations")
            if request.HasField("expected_price_minor") and request.expected_price_minor != int(
                flight["price"] * 100
            ):
                fail(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "Flight price changed; request a fresh quote",
                )
            if flight["available_seats"] < request.seat_count:
                fail(grpc.StatusCode.RESOURCE_EXHAUSTED, "Not enough available seats")
            db.execute(
                text("UPDATE flights SET available_seats=available_seats-:count WHERE id=:id"),
                {"id": flight_id, "count": request.seat_count},
            )
            row = (
                db.execute(
                    text(
                        "INSERT INTO seat_reservations(id, flight_id, booking_id, seat_count, unit_price)"
                        " VALUES (:id, :flight, :booking, :count, :price) RETURNING *"
                    ),
                    {
                        "id": uuid4(),
                        "flight": flight_id,
                        "booking": booking_id,
                        "count": request.seat_count,
                        "price": flight["price"],
                    },
                )
                .mappings()
                .one()
            )
            old_revision = self.cache.revision(db)
            self.cache.advance(db)
            result = reservation_message(row)
        self.cache.invalidate(old_revision)
        return result

    def ReleaseReservation(self, request, context):
        booking_id = uuid(request.booking_id)
        with self.engine.begin() as db:
            self.lock_booking(db, booking_id)
            db.execute(
                text(
                    "INSERT INTO reservation_cancellations(booking_id) VALUES (:id) ON CONFLICT DO NOTHING"
                ),
                {"id": booking_id},
            )
            row = (
                db.execute(
                    text("SELECT * FROM seat_reservations WHERE booking_id=:id"), {"id": booking_id}
                )
                .mappings()
                .first()
            )
            if row is None:
                return pb.ReleaseReservationResponse(released=True)
            if row["status"] != "ACTIVE":
                return pb.ReleaseReservationResponse(
                    released=True, reservation=reservation_message(row)
                )
            db.execute(
                text("SELECT id FROM flights WHERE id=:id FOR UPDATE"), {"id": row["flight_id"]}
            )
            db.execute(
                text("UPDATE flights SET available_seats=available_seats+:count WHERE id=:id"),
                {"id": row["flight_id"], "count": row["seat_count"]},
            )
            row = (
                db.execute(
                    text(
                        "UPDATE seat_reservations SET status='RELEASED' WHERE booking_id=:id RETURNING *"
                    ),
                    {"id": booking_id},
                )
                .mappings()
                .one()
            )
            old_revision = self.cache.revision(db)
            self.cache.advance(db)
            result = pb.ReleaseReservationResponse(
                released=True, reservation=reservation_message(row)
            )
        self.cache.invalidate(old_revision)
        return result

    def UpdateFlight(self, request, context):
        flight_id = uuid(request.flight_id)
        if not any(request.HasField(name) for name in ("price_minor", "total_seats", "status")):
            fail(grpc.StatusCode.INVALID_ARGUMENT, "No fields to update")
        if request.HasField("price_minor") and not 1 <= request.price_minor <= 999999999999:
            fail(grpc.StatusCode.INVALID_ARGUMENT, "Price is out of range")
        if request.HasField("total_seats") and request.total_seats <= 0:
            fail(grpc.StatusCode.INVALID_ARGUMENT, "Capacity must be positive")
        if request.HasField("status") and request.status not in (
            pb.SCHEDULED,
            pb.DEPARTED,
            pb.CANCELLED,
            pb.COMPLETED,
        ):
            fail(grpc.StatusCode.INVALID_ARGUMENT, "Invalid flight status")
        with self.engine.begin() as db:
            row = (
                db.execute(text("SELECT * FROM flights WHERE id=:id FOR UPDATE"), {"id": flight_id})
                .mappings()
                .first()
            )
            if row is None:
                fail(grpc.StatusCode.NOT_FOUND, "Flight not found")
            capacity = (
                request.total_seats if request.HasField("total_seats") else row["total_seats"]
            )
            available = row["available_seats"] + capacity - row["total_seats"]
            if available < 0:
                fail(
                    grpc.StatusCode.FAILED_PRECONDITION, "Capacity is below the reserved seat count"
                )
            db.execute(
                text(
                    "UPDATE flights SET total_seats=:capacity, available_seats=:available, price=:price, status=:status WHERE id=:id"
                ),
                {
                    "id": flight_id,
                    "capacity": capacity,
                    "available": available,
                    "price": Decimal(request.price_minor) / 100
                    if request.HasField("price_minor")
                    else row["price"],
                    "status": pb.FlightStatus.Name(request.status)
                    if request.HasField("status")
                    else row["status"],
                },
            )
            old_revision = self.cache.revision(db)
            self.cache.advance(db)
            result = flight_message(
                db.execute(text(FLIGHT_SELECT + " WHERE f.id=:id"), {"id": flight_id})
                .mappings()
                .one()
            )
        self.cache.invalidate(old_revision)
        return result
