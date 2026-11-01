CREATE TYPE flight_status AS ENUM ('SCHEDULED', 'DEPARTED', 'CANCELLED', 'COMPLETED');
CREATE TYPE reservation_status AS ENUM ('ACTIVE', 'RELEASED', 'EXPIRED');

CREATE TABLE airlines (
    id uuid PRIMARY KEY,
    code varchar(2) NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9]{2}$'),
    name varchar(120) NOT NULL CHECK (length(trim(name)) > 0)
);

CREATE TABLE flights (
    id uuid PRIMARY KEY,
    airline_id uuid NOT NULL REFERENCES airlines(id),
    flight_number varchar(10) NOT NULL CHECK (flight_number ~ '^[A-Z0-9]{2}[0-9]{1,4}$'),
    origin varchar(3) NOT NULL CHECK (origin ~ '^[A-Z]{3}$'),
    destination varchar(3) NOT NULL CHECK (destination ~ '^[A-Z]{3}$'),
    departure_time timestamptz NOT NULL,
    arrival_time timestamptz NOT NULL CHECK (arrival_time > departure_time),
    total_seats integer NOT NULL CHECK (total_seats > 0),
    available_seats integer NOT NULL CHECK (available_seats BETWEEN 0 AND total_seats),
    price numeric(12,2) NOT NULL CHECK (price > 0),
    status flight_status NOT NULL DEFAULT 'SCHEDULED',
    CHECK (origin <> destination)
);
CREATE UNIQUE INDEX flights_number_date ON flights
    (flight_number, ((departure_time AT TIME ZONE 'UTC')::date));
CREATE INDEX flights_search ON flights(origin, destination, departure_time)
    WHERE status = 'SCHEDULED';

CREATE TABLE seat_reservations (
    id uuid PRIMARY KEY,
    flight_id uuid NOT NULL REFERENCES flights(id),
    booking_id uuid NOT NULL UNIQUE,
    seat_count integer NOT NULL CHECK (seat_count > 0),
    unit_price numeric(12,2) NOT NULL CHECK (unit_price > 0),
    status reservation_status NOT NULL DEFAULT 'ACTIVE',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reservations_flight ON seat_reservations(flight_id);

-- Fences survive a release-before-reserve race and intentionally have no TTL.
CREATE TABLE reservation_cancellations (
    booking_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- A SQL-owned namespace makes old Redis data unreachable even after failover.
CREATE TABLE cache_revision (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision uuid NOT NULL
);
INSERT INTO cache_revision VALUES (true, gen_random_uuid());

INSERT INTO airlines VALUES
    ('10000000-0000-0000-0000-000000000001', 'SU', 'Aeroflot'),
    ('10000000-0000-0000-0000-000000000002', 'S7', 'S7 Airlines');
INSERT INTO flights VALUES
    ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001',
     'SU1001', 'SVO', 'LED', date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '7 days 10 hours',
     date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '7 days 11 hours 30 minutes',
     100, 100, 7500.00, 'SCHEDULED'),
    ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000002',
     'S72001', 'SVO', 'LED', date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '7 days 15 hours',
     date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '7 days 16 hours 40 minutes',
     50, 50, 6200.50, 'SCHEDULED'),
    ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001',
     'SU1002', 'LED', 'SVO', date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '8 days 10 hours',
     date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '8 days 11 hours 30 minutes',
     80, 80, 7300.00, 'SCHEDULED');
