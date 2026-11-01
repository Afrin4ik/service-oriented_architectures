#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
test_project="flight-booking-tests-$$"
compose() {
    docker compose -p "$test_project" -f docker-compose.yml -f compose.test.yml "$@"
}
cleanup() {
    test_result=$?
    trap - EXIT
    if [ "$test_result" -ne 0 ]; then
        compose logs --tail=50 booking flight
    fi
    compose --profile test down --volumes --remove-orphans
    exit "$test_result"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
compose build
compose up -d --wait --wait-timeout 120
compose run --rm --no-deps tests
