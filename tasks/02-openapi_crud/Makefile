.PHONY: up down generate test lint demo inspect
PYTHON ?= python

up:
	docker compose up --build --wait

down:
	docker compose down

generate:
	$(PYTHON) -m scripts.generate

test:
	sh scripts/test.sh

lint:
	$(PYTHON) -m ruff check app scripts tests

demo:
	$(PYTHON) -m scripts.demo

inspect:
	docker compose exec -T db psql -U marketplace -d marketplace -f /dev/stdin < scripts/inspect.sql
