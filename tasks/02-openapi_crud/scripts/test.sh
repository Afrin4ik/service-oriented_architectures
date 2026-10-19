#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
docker compose up -d --wait db
docker compose exec -T db psql -U marketplace -d postgres -v ON_ERROR_STOP=1 -c 'DROP DATABASE IF EXISTS marketplace_test WITH (FORCE)'
docker compose exec -T db psql -U marketplace -d postgres -v ON_ERROR_STOP=1 -c 'CREATE DATABASE marketplace_test'
docker compose run --rm -e FLYWAY_URL=jdbc:postgresql://db:5432/marketplace_test migrate
docker compose --profile test run --build --rm tests
