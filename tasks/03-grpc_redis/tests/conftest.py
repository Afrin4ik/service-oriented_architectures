import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import grpc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from booking.grpc_client import FlightGateway
from booking.main import create_app
from common.config import Settings
from common.db import database
from flight.cache import FlightCache
from flight.main import build_server
from flight.service import BusinessError, FlightService
from generated import flight_pb2 as pb
from generated import flight_pb2_grpc as rpc


class FaultFlightService(FlightService):
    def __init__(self, engine, cache):
        super().__init__(engine, cache)
        self.reserve_calls = 0
        self.before_failures = 0
        self.after_failures = 0
        self.release_failures = 0

    def ReserveSeats(self, request, context):
        self.reserve_calls += 1
        if self.before_failures:
            self.before_failures -= 1
            raise BusinessError(grpc.StatusCode.UNAVAILABLE, "Injected connection failure")
        result = super().ReserveSeats(request, context)
        if self.after_failures:
            self.after_failures -= 1
            raise BusinessError(
                grpc.StatusCode.DEADLINE_EXCEEDED, "Injected lost response after commit"
            )
        return result

    def ReleaseReservation(self, request, context):
        if self.release_failures:
            self.release_failures -= 1
            raise BusinessError(grpc.StatusCode.UNAVAILABLE, "Injected release failure")
        return super().ReleaseReservation(request, context)


@pytest.fixture
def system():
    if os.getenv("TESTING") != "1":
        pytest.skip("Integration tests require the isolated Docker test project: make test")
    settings = Settings()
    booking_engine, _ = database(settings.database_url)
    flight_engine, _ = database(os.environ["FLIGHT_DATABASE_URL"])
    with booking_engine.begin() as db:
        db.execute(text("TRUNCATE cancellation_jobs, bookings, booking_attempts"))
    with flight_engine.begin() as db:
        db.execute(text("TRUNCATE seat_reservations, reservation_cancellations, flights CASCADE"))
        db.execute(text("UPDATE cache_revision SET revision=:rev"), {"rev": uuid4()})
        flight_id = uuid4()
        departure = (datetime.now(UTC) + timedelta(days=30)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        db.execute(
            text(
                "INSERT INTO flights(id, airline_id, flight_number, origin, destination, departure_time, arrival_time, total_seats, available_seats, price)"
                " VALUES (:id, '10000000-0000-0000-0000-000000000001', 'SU1234', 'SVO', 'LED', :departure, :arrival, 5, 5, 1250.25)"
            ),
            {"id": flight_id, "departure": departure, "arrival": departure + timedelta(hours=2)},
        )
    cache = FlightCache(settings)
    servicer = FaultFlightService(flight_engine, cache)
    server, port = build_server(flight_engine, cache, settings.api_key, "127.0.0.1:0", servicer)
    server.start()
    settings = replace(settings, grpc_target=f"127.0.0.1:{port}", retry_backoff=0.001)
    gateway = FlightGateway(settings)
    channel = grpc.insecure_channel(settings.grpc_target)
    grpc.channel_ready_future(channel).result(timeout=3)
    app = create_app(settings, booking_engine, gateway, start_worker=False, run_migrations=False)
    with TestClient(app) as client:
        yield SimpleNamespace(
            settings=settings,
            booking_db=booking_engine,
            flight_db=flight_engine,
            cache=cache,
            servicer=servicer,
            gateway=gateway,
            channel=channel,
            stub=rpc.FlightServiceStub(channel),
            client=client,
            service=app.state.service,
            flight_id=str(flight_id),
            departure=departure,
            metadata=[("x-api-key", settings.api_key)],
            payload={
                "user_id": str(uuid4()),
                "flight_id": str(flight_id),
                "passenger_name": "Иван Петров",
                "passenger_email": "ivan@example.com",
                "seat_count": 2,
            },
        )
    gateway.close()
    channel.close()
    server.stop(0).wait()
    cache.close()
    flight_engine.dispose()
    booking_engine.dispose()


def rpc_call(system, method, request):
    return getattr(system.stub, method)(request, metadata=system.metadata, timeout=5)


def reserve_request(system, booking_id=None, count=1):
    return pb.ReserveSeatsRequest(
        flight_id=system.flight_id, booking_id=str(booking_id or uuid4()), seat_count=count
    )
