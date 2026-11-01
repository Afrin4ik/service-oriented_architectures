.PHONY: up down test demo resilience inspect generate lint

up:
	docker compose up --build -d --wait

down:
	docker compose down

test:
	sh scripts/test.sh

demo:
	docker compose exec booking python -m scripts.demo

resilience:
	python3 scripts/resilience.py

inspect:
	docker compose exec -T flight-db psql -U flight -d flights -c "SELECT * FROM alembic_version; SELECT id, flight_number, available_seats, total_seats, price, status FROM flights; SELECT booking_id, flight_id, seat_count, status FROM seat_reservations;"
	docker compose exec -T booking-db psql -U booking -d bookings -c "SELECT * FROM alembic_version; SELECT id, user_id, seat_count, total_price, status FROM bookings; SELECT id, status FROM booking_attempts; SELECT * FROM cancellation_jobs;"
	docker compose exec -T sentinel-1 redis-cli -p 26379 SENTINEL get-master-addr-by-name flight-cache

generate:
	.venv/bin/python -m scripts.generate

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
