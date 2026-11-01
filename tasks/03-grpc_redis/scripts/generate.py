import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            "-I",
            str(root / "proto"),
            f"--python_out={root / 'generated'}",
            f"--pyi_out={root / 'generated'}",
            f"--grpc_python_out={root / 'generated'}",
            str(root / "proto/flight.proto"),
        ],
        check=True,
    )
    path = root / "generated/flight_pb2_grpc.py"
    path.write_text(path.read_text().replace("import flight_pb2 as", "from . import flight_pb2 as"))


if __name__ == "__main__":
    main()
