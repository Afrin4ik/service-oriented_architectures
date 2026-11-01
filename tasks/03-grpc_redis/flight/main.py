import signal
import threading
from concurrent.futures import ThreadPoolExecutor

import grpc

from common.config import Settings
from common.db import database, migrate
from common.log import configure, event
from flight.cache import FlightCache
from flight.service import FlightService, GuardInterceptor
from generated import flight_pb2_grpc as rpc


def build_server(engine, cache, api_key, address, servicer=None):
    server = grpc.server(
        ThreadPoolExecutor(max_workers=20), interceptors=[GuardInterceptor(api_key)]
    )
    rpc.add_FlightServiceServicer_to_server(servicer or FlightService(engine, cache), server)
    port = server.add_insecure_port(address)
    if not port:
        raise RuntimeError("Cannot bind gRPC port")
    return server, port


def main():
    configure()
    settings = Settings()
    engine, _ = database(settings.database_url)
    migrate(engine, "flight")
    cache = FlightCache(settings)
    server, port = build_server(engine, cache, settings.api_key, f"[::]:{settings.grpc_port}")
    stopped = threading.Event()

    def stop(*_):
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.start()
    event("flight_started", port=port)
    stopped.wait()
    server.stop(grace=5).wait()
    cache.close()
    engine.dispose()


if __name__ == "__main__":
    main()
