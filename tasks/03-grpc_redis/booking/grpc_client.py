import threading
import time
from collections import deque, namedtuple

import grpc

from common.log import event
from generated import flight_pb2_grpc as rpc

TRANSIENT = {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(self, threshold, reset_seconds, window_seconds, clock=time.monotonic):
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.window_seconds = window_seconds
        self.clock = clock
        self.state = "CLOSED"
        self.failures = deque()
        self.opened_at = 0.0
        self.generation = 0
        self.probe_inflight = False
        self.lock = threading.Lock()

    def _transition(self, target):
        event("circuit_transition", previous=self.state, current=target)
        self.state = target
        self.generation += 1
        if target == "OPEN":
            self.opened_at = self.clock()
        if target == "CLOSED":
            self.failures.clear()
        self.probe_inflight = False

    def before_call(self):
        with self.lock:
            if self.state == "OPEN":
                if self.clock() - self.opened_at < self.reset_seconds:
                    raise CircuitOpenError(
                        "Flight service temporarily unavailable; circuit is OPEN"
                    )
                self._transition("HALF_OPEN")
            if self.state == "HALF_OPEN":
                if self.probe_inflight:
                    raise CircuitOpenError("Flight service recovery probe is in progress")
                self.probe_inflight = True
            return self.generation, self.state == "HALF_OPEN"

    def success(self, ticket):
        with self.lock:
            if ticket[0] == self.generation and self.state == "HALF_OPEN":
                self._transition("CLOSED")

    def failure(self, ticket):
        with self.lock:
            if ticket[0] != self.generation:
                return
            if self.state == "HALF_OPEN":
                self._transition("OPEN")
                return
            now = self.clock()
            while self.failures and now - self.failures[0] > self.window_seconds:
                self.failures.popleft()
            self.failures.append(now)
            if len(self.failures) >= self.threshold:
                self._transition("OPEN")

    def snapshot(self):
        with self.lock:
            return {
                "state": self.state,
                "failures_in_window": sum(
                    self.clock() - failure <= self.window_seconds for failure in self.failures
                ),
            }


class CallDetails(
    namedtuple("CallDetailsBase", "method timeout metadata credentials wait_for_ready compression"),
    grpc.ClientCallDetails,
):
    pass


class ResilienceInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, settings, breaker, sleep=time.sleep):
        self.settings = settings
        self.breaker = breaker
        self.sleep = sleep

    def intercept_unary_unary(self, continuation, details, request):
        ticket = self.breaker.before_call()
        metadata = [(key, value) for key, value in (details.metadata or ()) if key != "x-api-key"]
        metadata.append(("x-api-key", self.settings.api_key))
        call_details = CallDetails(
            details.method,
            min(details.timeout, self.settings.rpc_timeout)
            if details.timeout
            else self.settings.rpc_timeout,
            metadata,
            details.credentials,
            False,
            getattr(details, "compression", None),
        )
        # A HALF_OPEN probe is exactly one physical call.
        attempts = 1 if ticket[1] else 3
        for attempt in range(attempts):
            try:
                call = continuation(call_details, request)
                call.result()
                self.breaker.success(ticket)
                return call
            except grpc.RpcError as error:
                if error.code() not in TRANSIENT:
                    self.breaker.success(ticket)
                    raise
                if attempt + 1 == attempts:
                    self.breaker.failure(ticket)
                    raise
                delay = self.settings.retry_backoff * 2**attempt
                event(
                    "grpc_retry",
                    method=details.method,
                    attempt=attempt + 2,
                    delay_seconds=delay,
                    code=error.code().name,
                )
                self.sleep(delay)


class FlightGateway:
    def __init__(self, settings):
        self.breaker = CircuitBreaker(settings.cb_threshold, settings.cb_reset, settings.cb_window)
        self.channel = grpc.insecure_channel(
            settings.grpc_target,
            options=[
                ("grpc.enable_retries", 0),
                ("grpc.initial_reconnect_backoff_ms", 100),
                ("grpc.min_reconnect_backoff_ms", 100),
                ("grpc.max_reconnect_backoff_ms", 500),
            ],
        )
        self.intercepted = grpc.intercept_channel(
            self.channel, ResilienceInterceptor(settings, self.breaker)
        )
        self.stub = rpc.FlightServiceStub(self.intercepted)

    def close(self):
        self.intercepted.close()
