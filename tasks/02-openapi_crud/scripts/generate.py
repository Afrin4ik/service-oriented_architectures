import keyword
import subprocess
import sys
from pathlib import Path

import yaml
from openapi_spec_validator import validate

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "src/main/resources/openapi/marketplace.yaml"


def reference_name(schema):
    return schema["$ref"].rsplit("/", 1)[1]


def parameter_type(schema):
    if "$ref" in schema:
        schema = yaml.safe_load(SPEC.read_text())["components"]["schemas"][reference_name(schema)]
    if "enum" in schema:
        return f"Literal[{', '.join(repr(value) for value in schema['enum'])}]"
    if schema.get("format") == "uuid":
        return "UUID"
    return {"integer": "int", "string": "str", "boolean": "bool"}[schema["type"]]


def generate():
    spec = yaml.safe_load(SPEC.read_text())
    validate(spec)
    output = ROOT / "generated"
    output.mkdir(exist_ok=True)
    (output / "__init__.py").write_text("")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            "--input",
            str(SPEC),
            "--input-file-type",
            "openapi",
            "--output",
            str(output / "models.py"),
            "--output-model-type",
            "pydantic_v2.BaseModel",
            "--target-python-version",
            "3.12",
            "--base-class",
            "app.schema.ContractModel",
            "--enum-field-as-literal",
            "all",
            "--use-standard-collections",
            "--use-union-operator",
            "--use-default-kwarg",
            "--disable-timestamp",
            "--strict-nullable",
            "--collapse-root-models",
        ],
        check=True,
        cwd=ROOT,
    )
    lines = [
        "# Generated from marketplace.yaml by scripts/generate.py. Do not edit.",
        "from uuid import UUID",
        "from typing import Literal",
        "from fastapi import APIRouter, Depends, Path, Query, Request",
        "from generated import models",
        "from app.auth import require_roles",
        "from app.contract import ContractRoute",
        "from app.operations import execute",
        "",
        "router = APIRouter(route_class=ContractRoute)",
        "",
    ]
    for route, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            name = operation["operationId"]
            assert name.isidentifier() and not keyword.iskeyword(name)
            status = next(
                int(code)
                for code in operation["responses"]
                if code.isdigit() and code.startswith("2")
            )
            success = operation["responses"][str(status)]
            model = (
                "models." + reference_name(success["content"]["application/json"]["schema"])
                if "content" in success
                else "None"
            )
            signature = ["request: Request"]
            arguments = []
            if "requestBody" in operation:
                body = operation["requestBody"]["content"]["application/json"]["schema"]
                signature.append("body: models." + reference_name(body))
                arguments.append("body=body")
            for param in item.get("parameters", []) + operation.get("parameters", []):
                if "$ref" in param:
                    param = spec["components"]["parameters"][reference_name(param)]
                schema = param["schema"]
                annotation = parameter_type(schema)
                location = "Path" if param["in"] == "path" else "Query"
                required = param.get("required", False)
                default = "..." if required else repr(schema.get("default"))
                if not required and "default" not in schema:
                    annotation += " | None"
                constraints = []
                for key, target in {
                    "minimum": "ge",
                    "maximum": "le",
                    "minLength": "min_length",
                    "maxLength": "max_length",
                    "pattern": "pattern",
                }.items():
                    if key in schema:
                        constraints.append(f"{target}={schema[key]!r}")
                options = ", ".join([default, *constraints])
                signature.append(f"{param['name']}: {annotation} = {location}({options})")
                arguments.append(f"{param['name']}={param['name']}")
            roles = operation.get("x-roles")
            if roles:
                signature.append(f"actor: dict = Depends(require_roles({roles!r}))")
            lines += [
                f"@router.{method}({route!r}, operation_id={name!r}, status_code={status}, response_model={model})",
                f"def {name}(*, {', '.join(signature)}):",
                f"    return execute({name!r}, request, {'actor' if roles else 'None'}, response_type={model}{''.join(', ' + arg for arg in arguments)})",
                "",
            ]
    (output / "routes.py").write_text("\n".join(lines))
    print("Generated DTOs and FastAPI handlers from", SPEC.relative_to(ROOT))


if __name__ == "__main__":
    generate()
