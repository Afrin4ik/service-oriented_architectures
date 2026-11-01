from uuid import uuid4

from google.protobuf.message import DecodeError
from redis.backoff import NoBackoff
from redis.retry import Retry
from redis.sentinel import Sentinel
from sqlalchemy import text

from common.log import event
from redis import RedisError


class FlightCache:
    def __init__(self, settings):
        nodes = [
            (host, int(port))
            for host, port in (node.rsplit(":", 1) for node in settings.sentinel_nodes.split(","))
        ]
        self.sentinel = Sentinel(
            nodes,
            socket_timeout=0.2,
            socket_connect_timeout=0.2,
            sentinel_kwargs={"socket_timeout": 0.2, "socket_connect_timeout": 0.2},
        )
        self.client = self.sentinel.master_for(
            settings.sentinel_master,
            socket_timeout=0.2,
            socket_connect_timeout=0.2,
            retry=Retry(NoBackoff(), 0),
            health_check_interval=5,
        )
        self.ttl = settings.cache_ttl

    @staticmethod
    def revision(connection):
        return str(connection.execute(text("SELECT revision FROM cache_revision")).scalar_one())

    @staticmethod
    def advance(connection):
        return str(
            connection.execute(
                text("UPDATE cache_revision SET revision = :revision RETURNING revision"),
                {"revision": uuid4()},
            ).scalar_one()
        )

    def get(self, key, message_type):
        try:
            data = self.client.get(key)
            if data is not None:
                message = message_type.FromString(data)
                event("cache_hit", key=key)
                return message
        except (RedisError, ValueError, DecodeError) as error:
            event("cache_unavailable", operation="get", error=type(error).__name__)
        event("cache_miss", key=key)
        return None

    def put(self, key, message):
        try:
            self.client.set(key, message.SerializeToString(), ex=self.ttl)
        except RedisError as error:
            event("cache_unavailable", operation="set", error=type(error).__name__)

    def invalidate(self, old_revision):
        try:
            # All entries have a bounded lifetime; UNLINK only accelerates reclamation.
            for key in self.client.scan_iter(match=f"*:{old_revision}", count=100):
                self.client.unlink(key)
            event("cache_invalidated", revision=old_revision)
        except RedisError as error:
            event("cache_unavailable", operation="invalidate", error=type(error).__name__)

    def close(self):
        self.client.close()
        for sentinel in self.sentinel.sentinels:
            sentinel.close()
