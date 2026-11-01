import os
import sys

import grpc
import httpx

from generated import flight_pb2 as pb
from generated import flight_pb2_grpc as rpc


def main():
    if sys.argv[1] == "booking":
        httpx.get("http://127.0.0.1:8000/health", timeout=2).raise_for_status()
    else:
        with grpc.insecure_channel("127.0.0.1:50051") as channel:
            rpc.FlightServiceStub(channel).SearchFlights(
                pb.SearchFlightsRequest(origin="SVO", destination="LED"),
                timeout=2,
                metadata=[("x-api-key", os.environ["GRPC_API_KEY"])],
            )


if __name__ == "__main__":
    main()
