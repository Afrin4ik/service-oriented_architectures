import json
import logging
from datetime import UTC, datetime


def event(name, **values):
    logging.getLogger("flight_booking").info(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": name,
                **values,
            },
            default=str,
        )
    )


def configure():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
