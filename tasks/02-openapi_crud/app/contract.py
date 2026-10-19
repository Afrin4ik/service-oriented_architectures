import json
from decimal import Decimal
from pathlib import Path

import yaml
from fastapi.routing import APIRoute
from jsonschema import ValidationError, validators
from openapi_schema_validator import OAS30Validator

from app.errors import AppError

SPEC_PATH = Path(__file__).resolve().parents[1] / "src/main/resources/openapi/marketplace.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text())


def unique_by(validator, field, instance, schema):
    if not isinstance(instance, list):
        return
    seen = set()
    for index, item in enumerate(instance):
        if not isinstance(item, dict) or field not in item:
            continue
        value = str(item[field]).lower()
        if value in seen:
            yield ValidationError(f"Duplicate {field}", path=[index, field])
        seen.add(value)


ContractValidator = validators.extend(OAS30Validator, {"x-unique-by": unique_by})


def decimals(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: decimals(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decimals(item) for item in value]
    return value


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant {value}")


class ContractRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        operation = SPEC["paths"][self.path][next(iter(self.methods)).lower()]
        body_schema = (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        validator = ContractValidator(
            decimals({**(body_schema or {}), "components": SPEC["components"]}),
            format_checker=OAS30Validator.FORMAT_CHECKER,
        )

        async def validate(request):
            if body_schema is not None:
                violations = []
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    violations.append(
                        {"field": "body", "message": "Content-Type must be application/json"}
                    )
                else:
                    try:
                        value = json.loads(
                            await request.body(),
                            parse_float=Decimal,
                            parse_constant=reject_constant,
                        )
                        violations = [
                            {
                                "field": ".".join(["body", *map(str, error.absolute_path)]),
                                "message": error.message,
                            }
                            for error in validator.iter_errors(value)
                        ]
                    except (ValueError, UnicodeDecodeError):
                        violations = [{"field": "body", "message": "Malformed JSON"}]
                if violations:
                    raise AppError(
                        400, "VALIDATION_ERROR", "Invalid input", {"violations": violations}
                    )
            return await handler(request)

        return validate
