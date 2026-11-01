import json
import sys
from uuid import uuid4

from common.config import Settings
from flight.cache import FlightCache


def main():
    settings = Settings()
    cache = FlightCache(settings)
    try:
        host, port = cache.sentinel.discover_master(settings.sentinel_master)
        key = f"probe:{uuid4()}"
        if "--wait-replica" in sys.argv:
            wait_client = cache.sentinel.master_for(
                settings.sentinel_master,
                socket_timeout=2,
                socket_connect_timeout=0.2,
            )
            try:
                results = (
                    wait_client.pipeline(transaction=False)
                    .set(key, b"ok", ex=300)
                    .wait(1, 1000)
                    .execute()
                )
            finally:
                wait_client.close()
            assert results[1] >= 1, "Replica has not synchronized yet"
        else:
            cache.client.set(key, b"ok", ex=300)
        assert cache.client.get(key) == b"ok"
        assert 0 < cache.client.ttl(key) <= 300
        print(json.dumps({"master": host, "port": port, "cache": "ok"}))
    finally:
        cache.close()


if __name__ == "__main__":
    main()
