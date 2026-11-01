import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("BOOKING_URL")


def compose(*arguments):
    return subprocess.run(
        ["docker", "compose", *arguments],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def get(path):
    try:
        with urlopen(BASE + path, timeout=8) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def eventually(check, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (subprocess.CalledProcessError, URLError, TimeoutError):
            pass
        time.sleep(0.5)
    raise AssertionError("Recovery deadline exceeded")


def probe(*args):
    return json.loads(compose("exec", "-T", "flight", "python", "-m", "scripts.cache_probe", *args))


def main():
    global BASE
    if BASE is None:
        config = json.loads(compose("config", "--format", "json"))
        port = config["services"]["booking"]["ports"][0]["published"]
        BASE = f"http://127.0.0.1:{port}"
    status, flights = get("/flights?origin=SVO&destination=LED")
    assert status == 200 and flights
    path = "/flights/" + flights[0]["id"]
    print("Stopping Flight Service to exercise retry and circuit breaker", flush=True)
    try:
        compose("stop", "flight")
        for _ in range(50):
            status, _ = get(path)
            assert status == 503
            if get("/health")[1]["flight_circuit"]["state"] == "OPEN":
                break
        else:
            raise AssertionError("Circuit did not open")
        started = time.monotonic()
        status, error = get(path)
        elapsed = time.monotonic() - started
        assert status == 503 and error["error"]["code"] == "FLIGHT_CIRCUIT_OPEN"
        assert elapsed < 0.5, f"OPEN response took {elapsed:.3f}s"
        print(f"OPEN returned HTTP 503 in {elapsed:.3f}s", flush=True)
    finally:
        compose("start", "flight")
    eventually(lambda: get(path)[0] == 200)
    assert get("/health")[1]["flight_circuit"]["state"] == "CLOSED"
    print("Circuit recovered through HALF_OPEN to CLOSED", flush=True)

    old = eventually(lambda: probe("--wait-replica"))
    primary_service = next(
        service
        for service in ("redis-primary", "redis-replica")
        if compose("exec", "-T", service, "redis-cli", "--raw", "ROLE").splitlines()[0] == "master"
    )
    print(f"Stopping {primary_service}; Sentinel master was {old['master']}", flush=True)
    try:
        compose("stop", primary_service)
        for _ in range(3):
            assert get(path)[0] == 200, "PostgreSQL fallback must remain available"
        promoted = eventually(lambda: new_master(old))
        assert get(path)[0] == 200
        print(f"Sentinel promoted {promoted['master']}; cache read/write passed", flush=True)
    finally:
        compose("start", primary_service)
    eventually(lambda: probe("--wait-replica"))
    print("Resilience passed; stopped services have been restored", flush=True)


def new_master(old):
    current = probe()
    return current if current["master"] != old["master"] else None


if __name__ == "__main__":
    main()
