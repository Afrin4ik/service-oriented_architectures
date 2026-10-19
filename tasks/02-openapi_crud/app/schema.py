from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer


class ContractModel(BaseModel):
    """Shared serialization settings; all DTO fields come from OpenAPI."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    @field_serializer("*", when_used="json", mode="wrap", check_fields=False)
    def serialize_decimal(self, value, handler):
        return float(value) if isinstance(value, Decimal) else handler(value)
