from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import grpc
import pytest

from booking.grpc_client import CallDetails, CircuitBreaker, CircuitOpenError, ResilienceInterceptor


class RpcFailure(grpc.RpcError):
    def __init__(self, status):
        self.status = status

    def code(self):
        return self.status


def setup_interceptor(threshold=5):
    now = [0.0]
    breaker = CircuitBreaker(threshold, 10, 30, clock=lambda: now[0])
    sleeps = []
    interceptor = ResilienceInterceptor(
        SimpleNamespace(api_key="test-service-api-key", rpc_timeout=1, retry_backoff=0.1),
        breaker,
        sleep=sleeps.append,
    )
    details = CallDetails("/flight.v1.FlightService/GetFlight", None, (), None, False, None)
    return breaker, interceptor, details, now, sleeps


def outcome(value=None, code=None):
    future = Future()
    if code:
        future.set_exception(RpcFailure(code))
    else:
        future.set_result(value)
    return future


@pytest.mark.parametrize("code", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED])
def test_retry_three_attempts_and_exponential_backoff(code):
    breaker, interceptor, details, _, sleeps = setup_interceptor()
    calls = []

    def continuation(call_details, request):
        calls.append(call_details)
        return outcome(code=code)

    with pytest.raises(RpcFailure):
        interceptor.intercept_unary_unary(continuation, details, object())
    assert len(calls) == 3
    assert sleeps == [0.1, 0.2]
    assert list(breaker.failures) == [0.0]
    assert calls[0].timeout == 1
    assert ("x-api-key", "test-service-api-key") in calls[0].metadata


@pytest.mark.parametrize(
    "code",
    [
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.INTERNAL,
    ],
)
def test_business_errors_are_not_retried_or_counted(code):
    breaker, interceptor, details, _, sleeps = setup_interceptor()
    calls = []

    def continuation(*args):
        calls.append(1)
        return outcome(code=code)

    with pytest.raises(RpcFailure):
        interceptor.intercept_unary_unary(continuation, details, object())
    assert len(calls) == 1 and not sleeps and not breaker.failures


def test_retry_success_returns_result():
    breaker, interceptor, details, _, sleeps = setup_interceptor()
    outcomes = iter([outcome(code=grpc.StatusCode.UNAVAILABLE), outcome("ok")])
    result = interceptor.intercept_unary_unary(lambda *_: next(outcomes), details, None)
    assert result.result() == "ok" and sleeps == [0.1] and breaker.state == "CLOSED"


def test_circuit_open_half_open_single_probe_and_close():
    breaker, _, _, now, _ = setup_interceptor(threshold=2)
    breaker.failure(breaker.before_call())
    breaker.failure(breaker.before_call())
    assert breaker.state == "OPEN"
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    now[0] = 10
    probe = breaker.before_call()
    assert breaker.state == "HALF_OPEN"
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: probe_blocked(breaker), range(20)))
    assert all(results)
    breaker.success(probe)
    assert breaker.state == "CLOSED"


def probe_blocked(breaker):
    try:
        breaker.before_call()
        return False
    except CircuitOpenError:
        return True


def test_failed_half_open_probe_reopens_without_retry():
    breaker, interceptor, details, now, sleeps = setup_interceptor(threshold=1)
    breaker.failure(breaker.before_call())
    now[0] = 11
    calls = []

    def continuation(*args):
        calls.append(1)
        return outcome(code=grpc.StatusCode.UNAVAILABLE)

    with pytest.raises(RpcFailure):
        interceptor.intercept_unary_unary(continuation, details, None)
    assert len(calls) == 1 and sleeps == [] and breaker.state == "OPEN"


def test_open_interceptor_performs_zero_network_calls():
    breaker, interceptor, details, _, _ = setup_interceptor(threshold=1)
    breaker.failure(breaker.before_call())

    def forbidden(*args):
        pytest.fail("OPEN circuit must not call continuation")

    with pytest.raises(CircuitOpenError):
        interceptor.intercept_unary_unary(forbidden, details, None)


def test_window_and_stale_inflight_result():
    breaker, _, _, now, _ = setup_interceptor(threshold=2)
    old_ticket = breaker.before_call()
    breaker.failure(old_ticket)
    now[0] = 31
    breaker.failure(breaker.before_call())
    assert breaker.state == "CLOSED"
    breaker.failure(breaker.before_call())
    assert breaker.state == "OPEN"
    breaker.success(old_ticket)
    assert breaker.state == "OPEN"
    now[0] = 42
    probe = breaker.before_call()
    breaker.failure(old_ticket)
    assert breaker.state == "HALF_OPEN"
    breaker.success(probe)
    assert breaker.state == "CLOSED"
