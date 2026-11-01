from contextlib import asynccontextmanager
from datetime import UTC, datetime
from datetime import date as CalendarDate
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

import grpc
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from booking.grpc_client import CircuitOpenError, FlightGateway
from booking.service import BookingError, BookingService, RecoveryWorker
from common.config import Settings
from common.db import database, migrate
from common.log import configure, event
from generated import flight_pb2 as pb


class BookingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_id: UUID
    flight_id: UUID
    passenger_name: str = Field(min_length=1, max_length=200)
    passenger_email: EmailStr = Field(max_length=254)
    seat_count: int = Field(ge=1, le=10000, strict=True)


class BookingResponse(BaseModel):
    id: UUID
    user_id: UUID
    flight_id: UUID
    passenger_name: str
    passenger_email: str
    seat_count: int
    total_price: Decimal
    status: Literal["CONFIRMED", "CANCELLED"]
    created_at: datetime
    cancelled_at: datetime | None


class FlightResponse(BaseModel):
    id: UUID
    flight_number: str
    airline: str
    origin: str
    destination: str
    departure_time: datetime
    arrival_time: datetime
    total_seats: int
    available_seats: int
    price: Decimal
    currency: Literal["RUB"] = "RUB"
    status: Literal["SCHEDULED", "DEPARTED", "CANCELLED", "COMPLETED"]


def flight_response(flight):
    return FlightResponse(
        id=flight.id,
        flight_number=flight.flight_number,
        airline=flight.airline,
        origin=flight.origin,
        destination=flight.destination,
        departure_time=flight.departure_time.ToDatetime(tzinfo=UTC),
        arrival_time=flight.arrival_time.ToDatetime(tzinfo=UTC),
        total_seats=flight.total_seats,
        available_seats=flight.available_seats,
        price=(Decimal(flight.price_minor) / 100).quantize(Decimal("0.01")),
        status=pb.FlightStatus.Name(flight.status),
    )


def error_response(status, code, message):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(settings=None, engine=None, gateway=None, *, start_worker=True, run_migrations=True):
    configure()
    settings = settings or Settings()
    owned_engine, owned_gateway = engine is None, gateway is None
    engine = engine or database(settings.database_url)[0]
    gateway = gateway or FlightGateway(settings)
    service = BookingService(engine, gateway, settings)
    worker = RecoveryWorker(service, settings.recovery_interval)

    @asynccontextmanager
    async def lifespan(app):
        if run_migrations:
            migrate(engine, "booking")
        if start_worker:
            worker.start()
        event("booking_started")
        yield
        if start_worker:
            worker.stop()
        if owned_gateway:
            gateway.close()
        if owned_engine:
            engine.dispose()

    app = FastAPI(
        title="Flight Booking API",
        version="1.0.0",
        lifespan=lifespan,
        description="Bookings use RUB and UTC. Optional Idempotency-Key is a UUID. "
        "Cancellation intent is durable and retried after dependency recovery.",
    )
    app.state.service, app.state.gateway = service, gateway

    @app.exception_handler(BookingError)
    async def business_error(request, error):
        return error_response(error.status, error.code, str(error))

    @app.exception_handler(CircuitOpenError)
    async def circuit_error(request, error):
        return error_response(503, "FLIGHT_CIRCUIT_OPEN", str(error))

    @app.exception_handler(grpc.RpcError)
    async def grpc_error(request, error):
        statuses = {
            grpc.StatusCode.INVALID_ARGUMENT: 400,
            grpc.StatusCode.NOT_FOUND: 404,
            grpc.StatusCode.RESOURCE_EXHAUSTED: 409,
            grpc.StatusCode.ALREADY_EXISTS: 409,
            grpc.StatusCode.FAILED_PRECONDITION: 409,
            grpc.StatusCode.UNAVAILABLE: 503,
            grpc.StatusCode.DEADLINE_EXCEEDED: 504,
        }
        status = statuses.get(error.code(), 502)
        message = error.details() if status < 500 else "Flight service temporarily unavailable"
        return error_response(status, "FLIGHT_" + error.code().name, message)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        event("booking_database_error", error=type(error).__name__)
        return error_response(
            503, "BOOKING_DATABASE_UNAVAILABLE", "Booking database temporarily unavailable"
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return error_response(
            422,
            "VALIDATION_ERROR",
            "; ".join(
                ".".join(str(part) for part in item["loc"]) + ": " + item["msg"]
                for item in error.errors()
            ),
        )

    @app.get("/health")
    def health():
        with engine.connect() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "flight_circuit": gateway.breaker.snapshot()}

    @app.get("/flights", response_model=list[FlightResponse])
    def search_flights(
        origin: Annotated[str, Query(pattern="^[A-Z]{3}$")],
        destination: Annotated[str, Query(pattern="^[A-Z]{3}$")],
        date: CalendarDate | None = None,
    ):
        result = gateway.stub.SearchFlights(
            pb.SearchFlightsRequest(
                origin=origin,
                destination=destination,
                date=date.isoformat() if date else "",
            )
        )
        return [flight_response(flight) for flight in result.flights]

    @app.get("/flights/{flight_id}", response_model=FlightResponse)
    def get_flight(flight_id: UUID):
        return flight_response(gateway.stub.GetFlight(pb.GetFlightRequest(id=str(flight_id))))

    @app.post("/bookings", response_model=BookingResponse, status_code=201)
    def create_booking(
        body: BookingCreate, idempotency_key: Annotated[UUID | None, Header()] = None
    ):
        return service.create(body.model_dump(mode="json"), idempotency_key)

    @app.get("/bookings/{booking_id}", response_model=BookingResponse)
    def get_booking(booking_id: UUID):
        return service.get(booking_id)

    @app.post("/bookings/{booking_id}/cancel", response_model=BookingResponse)
    def cancel_booking(booking_id: UUID):
        return service.cancel(booking_id)

    @app.get("/bookings", response_model=list[BookingResponse])
    def list_bookings(user_id: UUID):
        return service.list(user_id)

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        response = await call_next(request)
        event(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
        )
        return response

    return app
