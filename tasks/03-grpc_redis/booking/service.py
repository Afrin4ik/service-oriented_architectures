import json
import threading
from decimal import Decimal
from uuid import uuid4

import grpc
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from booking.grpc_client import CircuitOpenError
from common.log import event
from generated import flight_pb2 as pb


class BookingError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code = status, code
        super().__init__(message)


class BookingService:
    def __init__(self, engine, gateway, settings):
        self.engine, self.gateway, self.settings = engine, gateway, settings

    def create(self, payload, booking_id=None):
        booking_id = booking_id or uuid4()
        # A successful HTTP replay returns the original price snapshot.
        with self.engine.connect() as db:
            existing = (
                db.execute(
                    text("SELECT payload, status FROM booking_attempts WHERE id=:id"),
                    {"id": booking_id},
                )
                .mappings()
                .first()
            )
            if existing:
                self.check_payload(existing, payload)
                if existing["status"] == "COMMITTED":
                    return self.get(booking_id)
                if existing["status"] in ("ABORTING", "ABORTED"):
                    raise BookingError(
                        409,
                        "ATTEMPT_ABORTED",
                        "Use a new Idempotency-Key for a new booking attempt",
                    )
        flight = self.gateway.stub.GetFlight(pb.GetFlightRequest(id=payload["flight_id"]))
        price = Decimal(flight.price_minor) / 100
        with self.engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO booking_attempts(id, payload, unit_price) VALUES (:id, CAST(:payload AS jsonb), :price)"
                    " ON CONFLICT DO NOTHING"
                ),
                {"id": booking_id, "payload": json.dumps(payload), "price": price},
            )
        compensate = False
        try:
            with self.engine.begin() as db:
                attempt = (
                    db.execute(
                        text("SELECT * FROM booking_attempts WHERE id=:id FOR UPDATE"),
                        {"id": booking_id},
                    )
                    .mappings()
                    .one()
                )
                self.check_payload(attempt, payload)
                if attempt["status"] == "COMMITTED":
                    return dict(
                        db.execute(text("SELECT * FROM bookings WHERE id=:id"), {"id": booking_id})
                        .mappings()
                        .one()
                    )
                if attempt["status"] != "PREPARING":
                    raise BookingError(409, "ATTEMPT_ABORTED", "Booking attempt was aborted")
                compensate = True
                reservation = self.gateway.stub.ReserveSeats(
                    pb.ReserveSeatsRequest(
                        flight_id=payload["flight_id"],
                        booking_id=str(booking_id),
                        seat_count=payload["seat_count"],
                        expected_price_minor=int(attempt["unit_price"] * 100),
                    )
                )
                if reservation.unit_price_minor != int(attempt["unit_price"] * 100):
                    raise BookingError(
                        409, "PRICE_CHANGED", "Reservation price differs from the quote"
                    )
                result = dict(
                    db.execute(
                        text(
                            "INSERT INTO bookings(id, user_id, flight_id, passenger_name, passenger_email, seat_count, total_price)"
                            " VALUES (:id, :user_id, :flight_id, :passenger_name, :passenger_email, :seat_count, :total) RETURNING *"
                        ),
                        {
                            **payload,
                            "id": booking_id,
                            "total": attempt["unit_price"] * payload["seat_count"],
                        },
                    )
                    .mappings()
                    .one()
                )
                db.execute(
                    text("UPDATE booking_attempts SET status='COMMITTED' WHERE id=:id"),
                    {"id": booking_id},
                )
            return result
        except Exception:
            # If commit acknowledgement was lost, the COMMITTED guard protects a
            # real booking. If DB is down, the durable PREPARING row is recovered.
            if compensate:
                self.abort(booking_id)
            raise

    @staticmethod
    def check_payload(attempt, payload):
        if attempt["payload"] != payload:
            raise BookingError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key is already bound to a different request",
            )

    def abort(self, booking_id):
        try:
            with self.engine.begin() as db:
                db.execute(
                    text(
                        "UPDATE booking_attempts SET status='ABORTING' WHERE id=:id AND status='PREPARING'"
                    ),
                    {"id": booking_id},
                )
            self.recover_attempt(booking_id)
        except (SQLAlchemyError, grpc.RpcError, CircuitOpenError) as error:
            event("compensation_pending", booking_id=booking_id, error=type(error).__name__)

    def recover_attempt(self, booking_id):
        with self.engine.begin() as db:
            row = db.execute(
                text("SELECT status FROM booking_attempts WHERE id=:id FOR UPDATE SKIP LOCKED"),
                {"id": booking_id},
            ).first()
            if row is None or row.status not in ("PREPARING", "ABORTING"):
                return
            self.gateway.stub.ReleaseReservation(
                pb.ReleaseReservationRequest(booking_id=str(booking_id))
            )
            db.execute(
                text("UPDATE booking_attempts SET status='ABORTED' WHERE id=:id"),
                {"id": booking_id},
            )
        event("compensation_completed", booking_id=booking_id)

    def get(self, booking_id):
        with self.engine.connect() as db:
            row = (
                db.execute(text("SELECT * FROM bookings WHERE id=:id"), {"id": booking_id})
                .mappings()
                .first()
            )
            if row is None:
                raise BookingError(404, "BOOKING_NOT_FOUND", "Booking not found")
            return dict(row)

    def list(self, user_id):
        with self.engine.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    text("SELECT * FROM bookings WHERE user_id=:id ORDER BY created_at, id"),
                    {"id": user_id},
                ).mappings()
            ]

    def cancel(self, booking_id):
        # Persist the intent before releasing remotely, to survive process death
        # between the two commits. Reads stay available while Flight is down.
        with self.engine.begin() as db:
            row = (
                db.execute(
                    text("SELECT * FROM bookings WHERE id=:id FOR UPDATE"), {"id": booking_id}
                )
                .mappings()
                .first()
            )
            if row is None:
                raise BookingError(404, "BOOKING_NOT_FOUND", "Booking not found")
            if row["status"] == "CANCELLED":
                return dict(row)
            db.execute(
                text(
                    "INSERT INTO cancellation_jobs(booking_id) VALUES (:id) ON CONFLICT DO NOTHING"
                ),
                {"id": booking_id},
            )
        self.finish_cancellation(booking_id)
        return self.get(booking_id)

    def finish_cancellation(self, booking_id):
        with self.engine.begin() as db:
            row = (
                db.execute(
                    text("SELECT * FROM bookings WHERE id=:id FOR UPDATE"), {"id": booking_id}
                )
                .mappings()
                .first()
            )
            if row is None or row["status"] == "CANCELLED":
                return
            self.gateway.stub.ReleaseReservation(
                pb.ReleaseReservationRequest(booking_id=str(booking_id))
            )
            db.execute(
                text("UPDATE bookings SET status='CANCELLED', cancelled_at=now() WHERE id=:id"),
                {"id": booking_id},
            )
            db.execute(
                text("DELETE FROM cancellation_jobs WHERE booking_id=:id"), {"id": booking_id}
            )
        event("booking_cancelled", booking_id=booking_id)

    def recover_once(self):
        with self.engine.connect() as db:
            attempts = list(
                db.execute(
                    text(
                        "SELECT id FROM booking_attempts WHERE status='ABORTING' OR"
                        " (status='PREPARING' AND created_at < now() - make_interval(secs => :grace))"
                        " ORDER BY created_at LIMIT 50"
                    ),
                    {"grace": self.settings.recovery_grace},
                ).scalars()
            )
            cancellations = list(
                db.execute(
                    text("SELECT booking_id FROM cancellation_jobs ORDER BY created_at LIMIT 50")
                ).scalars()
            )
        for booking_id in attempts:
            self.recover_attempt(booking_id)
        for booking_id in cancellations:
            self.finish_cancellation(booking_id)


class RecoveryWorker:
    def __init__(self, service, interval):
        self.service, self.interval = service, interval
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name="booking-recovery", daemon=True)

    def run(self):
        while not self.stopped.is_set():
            try:
                self.service.recover_once()
            except (SQLAlchemyError, grpc.RpcError, CircuitOpenError) as error:
                event("recovery_pending", error=type(error).__name__)
            self.stopped.wait(self.interval)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=10)
