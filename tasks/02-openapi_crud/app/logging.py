import json
import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

from app.errors import AppError, error_response

logger = logging.getLogger("marketplace.requests")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def redact(value):
    if isinstance(value, dict):
        return {
            key: "***"
            if any(word in key.lower() for word in ("password", "token", "secret", "authorization"))
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class RequestLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        timestamp = datetime.now(UTC).isoformat()
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        body = bytearray()
        status_code = 500
        response_started = False
        buffered = []
        mutable = scope["method"] in {"POST", "PUT", "DELETE", "PATCH"}
        if mutable:
            while True:
                message = await receive()
                buffered.append(message)
                if message["type"] == "http.request":
                    body.extend(message.get("body", b""))
                if message["type"] == "http.disconnect" or not message.get("more_body", False):
                    break

        async def read():
            if buffered:
                return buffered.pop(0)
            message = await receive()
            if not mutable and message["type"] == "http.request":
                body.extend(message.get("body", b""))
            return message

        async def write(message):
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        failure = None
        try:
            await self.app(scope, read, write)
        except Exception as exc:
            failure = type(exc).__name__
            if response_started:
                raise
            await error_response(AppError(500, "INTERNAL_ERROR", "Unexpected server error"))(
                scope, read, write
            )
        finally:
            entry = {
                "request_id": request_id,
                "method": scope["method"],
                "endpoint": scope["path"],
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "user_id": scope["state"].get("user_id"),
                "timestamp": timestamp,
            }
            if mutable:
                try:
                    decoded = json.loads(body) if body else None
                    json.dumps(decoded, allow_nan=False)
                    entry["request_body"] = redact(decoded)
                except (ValueError, UnicodeDecodeError):
                    entry["request_body"] = "[invalid JSON]"
            if failure:
                entry["exception_type"] = failure
            logger.info(json.dumps(entry, ensure_ascii=False, allow_nan=False))
