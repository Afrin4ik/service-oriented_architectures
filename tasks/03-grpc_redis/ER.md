# Схема данных и 3NF

Я разделил хранение на две независимые PostgreSQL: `flights` и `bookings`. Сервисы не читают чужую БД. Пунктирная связь между сервисами обозначает общий UUID, физического внешнего ключа между БД нет.

```mermaid
erDiagram
    AIRLINES ||--o{ FLIGHTS : operates
    FLIGHTS ||--o{ SEAT_RESERVATIONS : reserves
    BOOKINGS ||..|| SEAT_RESERVATIONS : "logical booking_id"
    BOOKINGS ||--o| CANCELLATION_JOBS : recovers

    AIRLINES {
        uuid id PK
        varchar code UK
        varchar name
    }
    FLIGHTS {
        uuid id PK
        uuid airline_id FK
        varchar flight_number "unique together with UTC departure date"
        varchar origin "IATA"
        varchar destination "IATA"
        timestamptz departure_time
        timestamptz arrival_time
        integer total_seats
        integer available_seats
        numeric price "12,2 RUB"
        flight_status status
    }
    SEAT_RESERVATIONS {
        uuid id PK
        uuid flight_id FK
        uuid booking_id UK "external reference"
        integer seat_count
        numeric unit_price "12,2 snapshot"
        reservation_status status
        timestamptz created_at
    }
    BOOKINGS {
        uuid id PK
        uuid user_id "external user ID"
        uuid flight_id "external flight ID"
        varchar passenger_name
        varchar passenger_email
        integer seat_count
        numeric total_price "16,2 snapshot"
        booking_status status
        timestamptz created_at
        timestamptz cancelled_at
    }
    CANCELLATION_JOBS {
        uuid booking_id PK,FK
        timestamptz created_at
    }
```

Технические таблицы восстановления и кеширования:

```mermaid
erDiagram
    BOOKING_ATTEMPTS {
        uuid id PK "future booking UUID and idempotency key"
        jsonb payload "immutable request command"
        numeric unit_price "quote snapshot"
        attempt_status status
        timestamptz created_at
    }
    RESERVATION_CANCELLATIONS {
        uuid booking_id PK "cancellation fence"
        timestamptz created_at
    }
    CACHE_REVISION {
        boolean singleton PK "always true"
        uuid revision "new UUID per inventory mutation"
    }
```

`booking_attempts` и `cancellation_jobs` находятся в Booking DB; `reservation_cancellations` и `cache_revision` — в Flight DB. Каждая БД также содержит собственную `alembic_version`.

В основных сущностях все значения атомарны, первичные ключи простые, неключевые атрибуты описывают соответствующий рейс, резервацию или бронирование. Название авиакомпании вынесено в `airlines`: зависимость `airline_id → name` не дублируется в `flights`. Таблица рейсов хранит только IATA-коды, без зависимых названий аэропортов. ФИО и email в Booking описывают пассажира конкретного бронирования, который может отличаться от владельца `user_id`.

Дата вылета отдельно не хранится: уникальность `(flight_number, UTC departure date)` задаёт индекс по выражению. В Booking хранится историческая итоговая цена, без копии текущей цены рейса и без отдельного вычисляемого `unit_price`. Цена в резервации также является историческим фактом, а не копией изменяемой цены рейса. JSONB в `booking_attempts` — журнал неизменяемых команд для восстановления; это не альтернативная модель бизнес-сущностей.

Ограничения закреплены в SQL-миграциях:

- `total_seats > 0`, `0 <= available_seats <= total_seats`, `seat_count > 0`, `price > 0`, `total_price > 0`;
- IATA соответствует `^[A-Z]{3}$`, аэропорты различаются, прилёт позже вылета;
- уникальны код авиакомпании, номер рейса с датой UTC и `seat_reservations.booking_id`;
- статусы ограничены PostgreSQL ENUM; `cancelled_at` заполнен тогда и только тогда, когда Booking отменён;
- локальные связи защищены FK, межсервисные операции — контрактом, идемпотентностью и журналом восстановления.

`available_seats` — явно требуемый условием счётчик. Я поддерживаю инвариант `available_seats + SUM(ACTIVE.seat_count) = total_seats` в одной транзакции под блокировкой рейса. Возврат мест и изменение статуса резервации коммитятся вместе.
