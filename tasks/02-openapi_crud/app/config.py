import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://marketplace:marketplace@localhost:5433/marketplace"
    )
    jwt_secret: str = os.environ.get("JWT_SECRET", "")
    jwt_issuer: str = "marketplace"
    jwt_audience: str = "marketplace-api"
    access_minutes: int = 20
    refresh_days: int = 14
    order_interval_minutes: Decimal = Decimal(os.environ.get("ORDER_INTERVAL_MINUTES", "1"))

    def __post_init__(self):
        if len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        if not self.order_interval_minutes.is_finite() or self.order_interval_minutes < 0:
            raise ValueError("ORDER_INTERVAL_MINUTES must be a nonnegative finite number")


settings = Settings()
