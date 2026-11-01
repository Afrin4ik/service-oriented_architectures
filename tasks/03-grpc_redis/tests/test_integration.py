import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import grpc
import httpx
import pytest
from conftest import reserve_request, rpc_call
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError, OperationalError

from generated import flight_pb2 as pb
from redis import ConnectionError as RedisConnectionError


def scalar(engine, sql, **params):
    with engine.connect() as db:
        return db.execute(text(sql), params).scalar_one()


def flight(system):
    return rpc_call(system, "GetFlight", pb.GetFlightRequest(id=system.flight_id))


@pytest.mark.parametrize(
    "method,message",
    [
        ("GetFlight", pb.GetFlightRequest()),
        ("SearchFlights", pb.SearchFlightsRequest()),
        ("ReserveSeats", pb.ReserveSeatsRequest()),
        ("ReleaseReservation", pb.ReleaseReservationRequest()),
        ("UpdateFlight", pb.UpdateFlightRequest()),
    ],
)
@pytest.mark.parametrize("metadata", [[], [("x-api-key", "wrong")]])
def test_authentication_all_methods(system, method, message, metadata):
    with pytest.raises(grpc.RpcError) as error:
        getattr(system.stub, method)(message, metadata=metadata, timeout=2)
    assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_search_optional_date_and_only_scheduled(system):
    request = pb.SearchFlightsRequest(origin="SVO", destination="LED")
    assert len(rpc_call(system, "SearchFlights", request).flights) == 1
    request.date = system.departure.date().isoformat()
    assert len(rpc_call(system, "SearchFlights", request).flights) == 1
    request.date = (system.departure.date() + timedelta(days=1)).isoformat()
    assert not rpc_call(system, "SearchFlights", request).flights
    rpc_call(
        system,
        "UpdateFlight",
        pb.UpdateFlightRequest(flight_id=system.flight_id, status=pb.CANCELLED),
    )
    request.date = ""
    assert not rpc_call(system, "SearchFlights", request).flights


@pytest.mark.parametrize(
    "origin,destination,date",
    [
        ("svo", "LED", ""),
        ("SVO", "SVO", ""),
        ("SVO", "LED", "2026-02-30"),
        ("SVO", "LED", "20260401"),
        ("SVO", "LED", "9999-12-31"),
    ],
)
def test_grpc_search_validation(system, origin, destination, date):
    with pytest.raises(grpc.RpcError) as error:
        rpc_call(
            system,
            "SearchFlights",
            pb.SearchFlightsRequest(origin=origin, destination=destination, date=date),
        )
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_missing_and_malformed_flight(system):
    for value, code in [
        (str(uuid4()), grpc.StatusCode.NOT_FOUND),
        ("invalid", grpc.StatusCode.INVALID_ARGUMENT),
    ]:
        with pytest.raises(grpc.RpcError) as error:
            rpc_call(system, "GetFlight", pb.GetFlightRequest(id=value))
        assert error.value.code() == code


def test_concurrent_last_seats_no_overselling(system):
    def reserve(_):
        try:
            rpc_call(system, "ReserveSeats", reserve_request(system))
            return "ok"
        except grpc.RpcError as error:
            return error.code()

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(reserve, range(12)))
    assert results.count("ok") == 5
    assert results.count(grpc.StatusCode.RESOURCE_EXHAUSTED) == 7
    assert flight(system).available_seats == 0
    assert scalar(system.flight_db, "SELECT count(*) FROM seat_reservations") == 5


def test_concurrent_same_booking_is_idempotent(system):
    request = reserve_request(system)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: rpc_call(system, "ReserveSeats", request), range(12)))
    assert len({result.id for result in results}) == 1
    assert flight(system).available_seats == 4
    request.seat_count = 2
    with pytest.raises(grpc.RpcError) as error:
        rpc_call(system, "ReserveSeats", request)
    assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS


def test_release_concurrent_and_fence(system):
    request = reserve_request(system, count=3)
    rpc_call(system, "ReserveSeats", request)
    release = pb.ReleaseReservationRequest(booking_id=request.booking_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: rpc_call(system, "ReleaseReservation", release), range(12)))
    assert flight(system).available_seats == 5
    assert scalar(system.flight_db, "SELECT status FROM seat_reservations") == "RELEASED"
    with pytest.raises(grpc.RpcError) as error:
        rpc_call(system, "ReserveSeats", request)
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_release_before_delayed_reserve(system):
    request = reserve_request(system)
    rpc_call(
        system, "ReleaseReservation", pb.ReleaseReservationRequest(booking_id=request.booking_id)
    )
    with pytest.raises(grpc.RpcError) as error:
        rpc_call(system, "ReserveSeats", request)
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert flight(system).available_seats == 5


@pytest.mark.parametrize(
    "assignment",
    [
        "available_seats=-1",
        "total_seats=0",
        "price=0",
        "origin='bad'",
        "arrival_time=departure_time",
        "available_seats=total_seats+1",
    ],
)
def test_postgres_constraints(system, assignment):
    with pytest.raises(IntegrityError), system.flight_db.begin() as db:
        db.execute(text("UPDATE flights SET " + assignment))
    assert flight(system).available_seats == 5


def test_number_and_utc_departure_date_unique(system):
    with pytest.raises(IntegrityError), system.flight_db.begin() as db:
        db.execute(
            text(
                "INSERT INTO flights SELECT :id, airline_id, flight_number, origin, destination,"
                " departure_time+interval '1 hour', arrival_time+interval '1 hour', total_seats, available_seats, price, status FROM flights"
            ),
            {"id": uuid4()},
        )


def test_http_booking_lifecycle_price_snapshot_and_replay(system):
    key = str(uuid4())
    headers = {"Idempotency-Key": key}
    response = system.client.post("/bookings", json=system.payload, headers=headers)
    assert response.status_code == 201, response.text
    booking = response.json()
    assert booking["total_price"] == "2500.50" and booking["status"] == "CONFIRMED"
    assert flight(system).available_seats == 3
    rpc_call(
        system,
        "UpdateFlight",
        pb.UpdateFlightRequest(flight_id=system.flight_id, price_minor=999900),
    )
    assert system.client.post("/bookings", json=system.payload, headers=headers).json() == booking
    assert system.client.get(f"/bookings/{key}").json() == booking
    assert system.client.get("/bookings", params={"user_id": system.payload["user_id"]}).json() == [
        booking
    ]
    conflict = system.client.post(
        "/bookings", json={**system.payload, "seat_count": 1}, headers=headers
    )
    assert conflict.status_code == 409
    for _ in range(2):
        cancelled = system.client.post(f"/bookings/{key}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    assert flight(system).available_seats == 5
    assert system.client.get(f"/bookings/{uuid4()}").status_code == 404
    assert system.client.post(f"/bookings/{uuid4()}/cancel").status_code == 404


@pytest.mark.parametrize(
    "override",
    [
        {"seat_count": 0},
        {"seat_count": -1},
        {"seat_count": True},
        {"passenger_name": "  "},
        {"passenger_email": "broken"},
        {"flight_id": "not-a-uuid"},
        {"unexpected": 1},
    ],
)
def test_rest_validation(system, override):
    response = system.client.post("/bookings", json={**system.payload, **override})
    assert response.status_code == 422
    assert scalar(system.booking_db, "SELECT count(*) FROM bookings") == 0


def test_failed_reservation_creates_no_booking(system):
    response = system.client.post("/bookings", json={**system.payload, "seat_count": 6})
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "FLIGHT_RESOURCE_EXHAUSTED"
    )
    assert system.servicer.reserve_calls == 1
    assert scalar(system.booking_db, "SELECT count(*) FROM bookings") == 0
    assert flight(system).available_seats == 5
    response = system.client.post("/bookings", json={**system.payload, "flight_id": str(uuid4())})
    assert response.status_code == 404


def test_transient_retries_and_lost_ack_are_idempotent(system):
    system.servicer.before_failures = 1
    system.servicer.after_failures = 1
    response = system.client.post("/bookings", json=system.payload)
    assert response.status_code == 201, response.text
    assert system.servicer.reserve_calls == 3
    assert scalar(system.flight_db, "SELECT count(*) FROM seat_reservations") == 1
    assert flight(system).available_seats == 3


def test_all_reservation_responses_lost_compensates(system):
    system.servicer.after_failures = 3
    response = system.client.post("/bookings", json=system.payload)
    assert response.status_code == 504
    assert system.servicer.reserve_calls == 3
    assert scalar(system.booking_db, "SELECT count(*) FROM bookings") == 0
    assert scalar(system.booking_db, "SELECT status FROM booking_attempts") == "ABORTED"
    assert scalar(system.flight_db, "SELECT status FROM seat_reservations") == "RELEASED"
    assert flight(system).available_seats == 5


def test_booking_sql_failure_after_reserve_compensates(system):
    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO bookings(" in statement:
            raise OperationalError(statement, parameters, RuntimeError("Injected SQL failure"))

    event.listen(system.booking_db, "before_cursor_execute", fail_insert)
    try:
        response = system.client.post("/bookings", json=system.payload)
    finally:
        event.remove(system.booking_db, "before_cursor_execute", fail_insert)
    assert response.status_code == 503
    assert scalar(system.booking_db, "SELECT count(*) FROM bookings") == 0
    assert flight(system).available_seats == 5
    assert scalar(system.booking_db, "SELECT status FROM booking_attempts") == "ABORTED"


@pytest.mark.parametrize("reserved", [False, True])
def test_durable_recovery_after_process_crash(system, reserved):
    booking_id = uuid4()
    with system.booking_db.begin() as db:
        db.execute(
            text(
                "INSERT INTO booking_attempts(id,payload,unit_price,created_at)"
                " VALUES (:id,CAST(:payload AS jsonb),1250.25,now()-interval '1 hour')"
            ),
            {"id": booking_id, "payload": json.dumps(system.payload)},
        )
    request = reserve_request(system, booking_id, 2)
    if reserved:
        rpc_call(system, "ReserveSeats", request)
    system.service.recover_once()
    assert flight(system).available_seats == 5
    assert scalar(system.booking_db, "SELECT status FROM booking_attempts") == "ABORTED"
    with pytest.raises(grpc.RpcError):
        rpc_call(system, "ReserveSeats", request)


def test_cancel_failure_is_recovered_from_durable_job(system):
    booking = system.client.post("/bookings", json=system.payload).json()
    system.servicer.release_failures = 3
    response = system.client.post(f"/bookings/{booking['id']}/cancel")
    assert response.status_code == 503
    assert scalar(system.booking_db, "SELECT status FROM bookings") == "CONFIRMED"
    assert scalar(system.booking_db, "SELECT count(*) FROM cancellation_jobs") == 1
    system.service.recover_once()
    assert scalar(system.booking_db, "SELECT status FROM bookings") == "CANCELLED"
    assert scalar(system.booking_db, "SELECT count(*) FROM cancellation_jobs") == 0
    assert flight(system).available_seats == 5


def test_cache_hit_ttl_and_mutations(system, caplog):
    caplog.set_level("INFO", logger="flight_booking")
    flight(system)
    flight(system)
    request = pb.SearchFlightsRequest(origin="SVO", destination="LED")
    rpc_call(system, "SearchFlights", request)
    rpc_call(system, "SearchFlights", request)
    with system.flight_db.connect() as db:
        revision = system.cache.revision(db)
    keys = list(system.cache.client.scan_iter(match=f"*:{revision}"))
    assert len(keys) == 2
    assert all(0 < system.cache.client.ttl(key) <= 300 for key in keys)
    assert '"cache_hit"' in caplog.text and '"cache_miss"' in caplog.text
    reserve = reserve_request(system)
    rpc_call(system, "ReserveSeats", reserve)
    assert flight(system).available_seats == 4
    assert rpc_call(system, "SearchFlights", request).flights[0].available_seats == 4
    rpc_call(
        system, "ReleaseReservation", pb.ReleaseReservationRequest(booking_id=reserve.booking_id)
    )
    assert flight(system).available_seats == 5
    rpc_call(
        system,
        "UpdateFlight",
        pb.UpdateFlightRequest(flight_id=system.flight_id, price_minor=555500),
    )
    assert flight(system).price_minor == 555500


def test_redis_outage_and_lost_invalidation_cannot_serve_old_cache(system, monkeypatch):
    assert flight(system).available_seats == 5
    with monkeypatch.context() as patch:
        patch.setattr(system.cache, "invalidate", lambda _: None)
        patch.setattr(
            system.cache.client, "get", lambda *_: (_ for _ in ()).throw(RedisConnectionError())
        )
        patch.setattr(
            system.cache.client,
            "set",
            lambda *_, **__: (_ for _ in ()).throw(RedisConnectionError()),
        )
        rpc_call(system, "ReserveSeats", reserve_request(system))
        assert flight(system).available_seats == 4
    assert flight(system).available_seats == 4


def test_stale_cache_fill_racing_with_mutation_is_unreachable(system, monkeypatch):
    started, proceed = threading.Event(), threading.Event()
    original_put = system.cache.put

    def delayed_put(key, value):
        if key.startswith("flight:"):
            started.set()
            assert proceed.wait(4)
        original_put(key, value)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(system.cache, "put", delayed_put)
        pending = pool.submit(flight, system)
        assert started.wait(3)
        rpc_call(
            system,
            "UpdateFlight",
            pb.UpdateFlightRequest(flight_id=system.flight_id, price_minor=111100),
        )
        proceed.set()
        assert pending.result().price_minor == 125025
    assert flight(system).price_minor == 111100


def test_capacity_and_price_change_protection(system):
    rpc_call(system, "ReserveSeats", reserve_request(system, count=4))
    with pytest.raises(grpc.RpcError) as error:
        rpc_call(
            system,
            "UpdateFlight",
            pb.UpdateFlightRequest(flight_id=system.flight_id, total_seats=3),
        )
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    request = reserve_request(system)
    request.expected_price_minor = 1
    with pytest.raises(grpc.RpcError) as error:
        rpc_call(system, "ReserveSeats", request)
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert flight(system).available_seats == 1


def test_real_running_http_to_grpc_and_two_databases(system):
    with httpx.Client(base_url=os.environ["BOOKING_URL"], timeout=10) as client:
        assert client.get("/health").status_code == 200
        assert client.get(f"/flights/{system.flight_id}").json()["price"] == "1250.25"
        response = client.post("/bookings", json=system.payload)
        assert response.status_code == 201, response.text
        booking = response.json()
        assert (
            scalar(
                system.flight_db,
                "SELECT count(*) FROM seat_reservations WHERE booking_id=:id",
                id=booking["id"],
            )
            == 1
        )
        assert client.post(f"/bookings/{booking['id']}/cancel").json()["status"] == "CANCELLED"
        assert flight(system).available_seats == 5


def test_codegen_reproducible():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "generated" / name
        for name in ("flight_pb2.py", "flight_pb2_grpc.py", "flight_pb2.pyi")
    ]
    before = [path.read_bytes() for path in paths]
    subprocess.run([sys.executable, "-m", "scripts.generate"], cwd=root, check=True)
    assert before == [path.read_bytes() for path in paths]


def test_concurrent_http_bookings_for_last_seat(system):
    rpc_call(
        system, "UpdateFlight", pb.UpdateFlightRequest(flight_id=system.flight_id, total_seats=1)
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda _: system.client.post("/bookings", json={**system.payload, "seat_count": 1}),
                range(8),
            )
        )
    assert sum(response.status_code == 201 for response in responses) == 1
    assert sum(response.status_code == 409 for response in responses) == 7
    assert scalar(system.booking_db, "SELECT count(*) FROM bookings") == 1
    assert (
        scalar(system.flight_db, "SELECT count(*) FROM seat_reservations WHERE status='ACTIVE'")
        == 1
    )
    assert flight(system).available_seats == 0


def test_flight_insert_failure_rolls_back_seat_decrement(system):
    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO seat_reservations(" in statement:
            raise OperationalError(
                statement, parameters, RuntimeError("Injected reservation SQL failure")
            )

    event.listen(system.flight_db, "before_cursor_execute", fail_insert)
    try:
        response = system.client.post("/bookings", json=system.payload)
    finally:
        event.remove(system.flight_db, "before_cursor_execute", fail_insert)
    assert response.status_code == 503
    assert system.servicer.reserve_calls == 3
    assert flight(system).available_seats == 5
    assert scalar(system.flight_db, "SELECT count(*) FROM seat_reservations") == 0
    assert scalar(system.booking_db, "SELECT count(*) FROM bookings") == 0


def test_failed_compensation_is_durably_retried(system):
    system.servicer.after_failures = 3
    system.servicer.release_failures = 3
    response = system.client.post("/bookings", json=system.payload)
    assert response.status_code == 504
    assert scalar(system.booking_db, "SELECT status FROM booking_attempts") == "ABORTING"
    assert flight(system).available_seats == 3
    system.service.recover_once()
    assert scalar(system.booking_db, "SELECT status FROM booking_attempts") == "ABORTED"
    assert flight(system).available_seats == 5


def test_lost_booking_commit_ack_never_compensates_committed_booking(system, monkeypatch):
    key = str(uuid4())
    lose_ack = [False]
    real_begin = system.booking_db.begin

    def committed_statement(conn, cursor, statement, parameters, context, executemany):
        if "UPDATE booking_attempts SET status='COMMITTED'" in statement:
            lose_ack[0] = True

    @contextmanager
    def ambiguous_begin():
        with real_begin() as db:
            yield db
        if lose_ack[0]:
            lose_ack[0] = False
            raise OperationalError("COMMIT", {}, RuntimeError("Lost commit acknowledgement"))

    event.listen(system.booking_db, "before_cursor_execute", committed_statement)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(system.booking_db, "begin", ambiguous_begin)
            response = system.client.post(
                "/bookings", json=system.payload, headers={"Idempotency-Key": key}
            )
    finally:
        event.remove(system.booking_db, "before_cursor_execute", committed_statement)
    assert response.status_code == 503
    assert scalar(system.booking_db, "SELECT status FROM booking_attempts") == "COMMITTED"
    assert scalar(system.flight_db, "SELECT status FROM seat_reservations") == "ACTIVE"
    replay = system.client.post("/bookings", json=system.payload, headers={"Idempotency-Key": key})
    assert replay.status_code == 201 and replay.json()["id"] == key
    assert flight(system).available_seats == 3


def test_cancellation_commit_failure_recovers_without_double_release(system):
    booking = system.client.post("/bookings", json=system.payload).json()

    def fail_update(conn, cursor, statement, parameters, context, executemany):
        if "UPDATE bookings SET status='CANCELLED'" in statement:
            raise OperationalError(
                statement, parameters, RuntimeError("Injected cancellation SQL failure")
            )

    event.listen(system.booking_db, "before_cursor_execute", fail_update)
    try:
        response = system.client.post(f"/bookings/{booking['id']}/cancel")
    finally:
        event.remove(system.booking_db, "before_cursor_execute", fail_update)
    assert response.status_code == 503
    assert scalar(system.booking_db, "SELECT count(*) FROM cancellation_jobs") == 1
    assert scalar(system.booking_db, "SELECT status FROM bookings") == "CONFIRMED"
    assert flight(system).available_seats == 5
    system.service.recover_once()
    assert scalar(system.booking_db, "SELECT status FROM bookings") == "CANCELLED"
    assert flight(system).available_seats == 5
