import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.environ["DATABASE_URL"])
    api_key: str = field(default_factory=lambda: os.environ["GRPC_API_KEY"])
    grpc_target: str = field(
        default_factory=lambda: os.getenv("FLIGHT_GRPC_TARGET", "flight:50051")
    )
    grpc_port: int = field(default_factory=lambda: int(os.getenv("GRPC_PORT", "50051")))
    rpc_timeout: float = field(
        default_factory=lambda: float(os.getenv("GRPC_TIMEOUT_SECONDS", "1"))
    )
    retry_backoff: float = field(
        default_factory=lambda: float(os.getenv("RETRY_BACKOFF_SECONDS", "0.1"))
    )
    cb_threshold: int = field(default_factory=lambda: int(os.getenv("CB_FAILURE_THRESHOLD", "5")))
    cb_reset: float = field(default_factory=lambda: float(os.getenv("CB_RESET_SECONDS", "10")))
    cb_window: float = field(default_factory=lambda: float(os.getenv("CB_WINDOW_SECONDS", "30")))
    sentinel_nodes: str = field(
        default_factory=lambda: os.getenv(
            "REDIS_SENTINELS", "sentinel-1:26379,sentinel-2:26379,sentinel-3:26379"
        )
    )
    sentinel_master: str = field(
        default_factory=lambda: os.getenv("REDIS_MASTER_NAME", "flight-cache")
    )
    cache_ttl: int = field(default_factory=lambda: int(os.getenv("CACHE_TTL_SECONDS", "300")))
    recovery_interval: float = field(
        default_factory=lambda: float(os.getenv("RECOVERY_INTERVAL_SECONDS", "2"))
    )
    recovery_grace: float = field(
        default_factory=lambda: float(os.getenv("RECOVERY_GRACE_SECONDS", "30"))
    )

    def __post_init__(self):
        if not self.api_key or len(self.api_key) < 16:
            raise ValueError("GRPC_API_KEY must contain at least 16 characters")
        if not 300 <= self.cache_ttl <= 600:
            raise ValueError("CACHE_TTL_SECONDS must be between 300 and 600")
        if (
            min(
                self.rpc_timeout,
                self.cb_threshold,
                self.cb_reset,
                self.cb_window,
                self.recovery_interval,
                self.recovery_grace,
            )
            <= 0
            or self.retry_backoff < 0
        ):
            raise ValueError("Timeouts and thresholds must be positive")
