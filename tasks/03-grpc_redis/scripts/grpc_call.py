import argparse
import json
import os

import grpc
from google.protobuf.json_format import MessageToDict, ParseDict

from generated import flight_pb2 as pb
from generated import flight_pb2_grpc as rpc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "method",
        choices=[
            "SearchFlights",
            "GetFlight",
            "ReserveSeats",
            "ReleaseReservation",
            "UpdateFlight",
        ],
    )
    parser.add_argument("body", help="JSON request using protobuf field names")
    parser.add_argument("--target", default="flight:50051")
    parser.add_argument("--key", default=os.getenv("GRPC_API_KEY", ""))
    parser.add_argument("--no-auth", action="store_true")
    args = parser.parse_args()
    request = ParseDict(json.loads(args.body), getattr(pb, args.method + "Request")())
    metadata = [] if args.no_auth else [("x-api-key", args.key)]
    with grpc.insecure_channel(args.target) as channel:
        try:
            result = getattr(rpc.FlightServiceStub(channel), args.method)(
                request, metadata=metadata, timeout=3
            )
        except grpc.RpcError as error:
            print(json.dumps({"code": error.code().name, "message": error.details()}))
            raise SystemExit(1) from None
    print(
        json.dumps(
            MessageToDict(result, preserving_proto_field_name=True), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
