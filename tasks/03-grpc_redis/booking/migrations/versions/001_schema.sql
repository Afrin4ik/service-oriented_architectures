CREATE TYPE booking_status AS ENUM ('CONFIRMED', 'CANCELLED');
CREATE TYPE attempt_status AS ENUM ('PREPARING', 'COMMITTED', 'ABORTING', 'ABORTED');

CREATE TABLE bookings (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    flight_id uuid NOT NULL,
    passenger_name varchar(200) NOT NULL CHECK (length(trim(passenger_name)) > 0),
    passenger_email varchar(254) NOT NULL CHECK (passenger_email ~ '^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$'),
    seat_count integer NOT NULL CHECK (seat_count > 0),
    total_price numeric(16,2) NOT NULL CHECK (total_price > 0),
    status booking_status NOT NULL DEFAULT 'CONFIRMED',
    created_at timestamptz NOT NULL DEFAULT now(),
    cancelled_at timestamptz,
    CHECK ((status = 'CANCELLED') = (cancelled_at IS NOT NULL))
);
CREATE INDEX bookings_user ON bookings(user_id, created_at, id);

-- Durable commands, not visible Booking entities. Written before remote mutation.
CREATE TABLE booking_attempts (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL,
    unit_price numeric(12,2) NOT NULL CHECK (unit_price > 0),
    status attempt_status NOT NULL DEFAULT 'PREPARING',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX attempts_pending ON booking_attempts(created_at)
    WHERE status IN ('PREPARING', 'ABORTING');

CREATE TABLE cancellation_jobs (
    booking_id uuid PRIMARY KEY REFERENCES bookings(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
