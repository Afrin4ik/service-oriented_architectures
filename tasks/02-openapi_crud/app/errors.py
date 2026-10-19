from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse


class AppError(Exception):
    def __init__(self, status, code, message, details=None, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}


def invalid(field, message):
    raise AppError(
        400,
        "VALIDATION_ERROR",
        "Invalid input",
        {"violations": [{"field": field, "message": message}]},
    )


def error_response(error):
    from generated.models import ErrorResponse

    dto = ErrorResponse(error_code=error.code, message=error.message, details=error.details)
    return JSONResponse(
        status_code=error.status,
        content=jsonable_encoder(dto, exclude_none=True),
        headers=error.headers,
    )
