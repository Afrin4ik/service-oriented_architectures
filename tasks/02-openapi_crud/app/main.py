from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from app.contract import SPEC
from app.errors import AppError, error_response
from app.logging import RequestLogMiddleware
from generated.routes import router

app = FastAPI(title=SPEC["info"]["title"], version=SPEC["info"]["version"])
app.include_router(router)
app.openapi = lambda: SPEC
app.add_middleware(RequestLogMiddleware)


@app.exception_handler(AppError)
async def application_error(request, error):
    return error_response(error)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    violations = [
        {"field": ".".join(map(str, item["loc"])), "message": item["msg"]}
        for item in error.errors()
    ]
    return error_response(
        AppError(400, "VALIDATION_ERROR", "Invalid input", {"violations": violations})
    )


@app.exception_handler(HTTPException)
async def http_error(request, error):
    code = {404: "ROUTE_NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(
        error.status_code, "VALIDATION_ERROR"
    )
    return error_response(
        AppError(error.status_code, code, str(error.detail), headers=error.headers)
    )
