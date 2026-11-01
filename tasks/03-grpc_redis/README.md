# Flight Booking: gRPC + Redis

Я реализовал систему бронирования на Python 3.12: Booking Service предоставляет REST API через FastAPI, Flight Service обслуживает gRPC-запросы. Каждый сервис хранит данные в собственной PostgreSQL. Flight Service использует Redis Sentinel: мастер, реплика и три Sentinel с кворумом 2.

Все файлы решения находятся в этой папке. Условие: [task-03.md](task-03.md), схема данных: [ER.md](ER.md).

## Запуск

Из корня репозитория:

```bash
cd tasks/03-grpc_redis
docker compose up --build -d --wait
```

Обычный `docker compose up` также собирает отсутствующий образ и запускает всю систему. Миграции Alembic применяются автоматически внутри каждого сервиса до открытия порта. Advisory lock сериализует миграции при одновременном старте нескольких экземпляров. В Flight DB создаются три демонстрационных рейса; дата первого вылета — через 7 дней от первого запуска БД. Повторный запуск сохраняет данные и не пересоздаёт рейсы.

| Адрес | Назначение |
| --- | --- |
| http://localhost:8003/docs | Swagger UI и REST-контракт |
| http://localhost:8003/openapi.json | OpenAPI, генерируемый из DTO и маршрутов FastAPI |
| http://localhost:8003/health | Booking DB и состояние Circuit Breaker |
| `flight:50051` внутри Compose | gRPC Flight Service |

На хост опубликован только REST-порт на loopback. PostgreSQL, Redis, Sentinel и gRPC доступны в сети Compose. Настройки по умолчанию предназначены для локального задания; переменные перечислены в [.env.example](.env.example). Можно скопировать файл в `.env`. Пароли БД при подстановке в URL должны состоять из URL-safe символов.

```bash
docker compose down
```

Эта команда останавливает систему с сохранением обеих PostgreSQL в именованных volumes.

## Соответствие всем десяти критериям

| Балл | Реализация | Где смотреть |
| --- | --- | --- |
| 1 | Бизнес-методы, Timestamp, enum, коды ошибок, генерация protoc | [proto/flight.proto](proto/flight.proto), [scripts/generate.py](scripts/generate.py) |
| 2 | Две схемы в 3NF, ER-диаграммы, CHECK, FK, UNIQUE | [ER.md](ER.md), SQL в `flight/migrations/versions` и `booking/migrations/versions` |
| 3 | Две PostgreSQL, автоматические миграции Alembic, REST и gRPC | [docker-compose.yml](docker-compose.yml), `booking/main.py`, `flight/main.py` |
| 4 | `GetFlight → ReserveSeats → INSERT Booking`, отмена через `ReleaseReservation` | [booking/service.py](booking/service.py) |
| 5 | `SELECT FOR UPDATE`, атомарные изменения остатков и резерваций, компенсация | [flight/service.py](flight/service.py), `booking/service.py` |
| 6 | API key из env в metadata, общий серверный interceptor для всех RPC | `flight/service.py:GuardInterceptor`, `booking/grpc_client.py` |
| 7 | Cache-Aside, TTL 300–600 секунд, инвалидация всех мутаций, hit/miss логи | [flight/cache.py](flight/cache.py), `flight/service.py` |
| 8 | До 3 попыток только UNAVAILABLE/DEADLINE_EXCEEDED, backoff, идемпотентность | [booking/grpc_client.py](booking/grpc_client.py), UNIQUE booking_id |
| 9 | Мастер + реплика + 3 Sentinel, клиент `Sentinel.master_for()` | `docker-compose.yml`, [redis/start-sentinel.sh](redis/start-sentinel.sh) |
| 10 | Circuit Breaker в клиентском interceptor, три состояния, один пробный RPC, HTTP 503 | `booking/grpc_client.py`, `/health`, `scripts/resilience.py` |

## Проверки

```bash
make test
make demo
make inspect
make resilience
```

`make test` создаёт отдельный Compose project с уникальным именем, двумя новыми БД и собственным Redis/Sentinel. Рабочие данные и REST-порт не используются. При завершении удаляются только контейнеры и volumes этого тестового проекта. Тесты работают с настоящими PostgreSQL, Redis и gRPC. В тестах ошибок запускается отдельный gRPC-сервер с управляемыми сбоями; бизнес-логика, SQL и транспорт остаются настоящими. Отдельный E2E-тест проходит через работающие HTTP- и gRPC-контейнеры.

Набор проверяет конкурентную покупку последних мест, одновременные повторы одного booking_id, повторную отмену, SQL-ограничения, все методы без credentials, снапшот цены, retry и backoff, Circuit Breaker, потерю ответа после коммита, ошибку SQL после резервирования, восстановление после остановки процесса и гонки заполнения кеша.

`make demo` проходит REST-сценарий создания, повторной отправки, чтения и отмены бронирования. `make inspect` показывает записи в обеих БД, версии миграций и текущий мастер Redis. `make resilience` временно останавливает Flight Service, проверяет быстрый 503 в OPEN и восстановление, затем останавливает текущий Redis-мастер и проверяет автоматическое переключение Sentinel. В `finally` скрипт запускает остановленные контейнеры. Для этой команды нужен Python 3 на хосте, дополнительных библиотек не требуется. Запускать демонстрацию отказов следует на этом учебном стенде без параллельных клиентских операций.

## REST-контракт

| Метод | URL | Результат |
| --- | --- | --- |
| GET | `/flights?origin=SVO&destination=LED&date=2026-12-01` | Только SCHEDULED, дата опциональна |
| GET | `/flights/{id}` | Рейс или 404 |
| POST | `/bookings` | 201, подтверждённое бронирование |
| GET | `/bookings/{id}` | Бронирование или 404 |
| POST | `/bookings/{id}/cancel` | CANCELLED, повтор идемпотентен |
| GET | `/bookings?user_id={uuid}` | Бронирования пользователя |

Все идентификаторы — UUID. Даты хранятся как `timestamptz`, календарная дата поиска интерпретируется в UTC. В REST деньги сериализуются строками с двумя знаками: `"7500.00"`. В БД используются NUMERIC и Python Decimal, в protobuf — целое количество копеек (`price_minor`), валюта RUB.

Пример:

```bash
curl -s 'http://localhost:8003/flights?origin=SVO&destination=LED'
curl -i http://localhost:8003/bookings \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 40000000-0000-0000-0000-000000000001' \
  -d '{"user_id":"30000000-0000-0000-0000-000000000001","flight_id":"20000000-0000-0000-0000-000000000001","passenger_name":"Иван Петров","passenger_email":"ivan@example.com","seat_count":2}'
```

`Idempotency-Key` опционален. Если он задан, его UUID становится ID бронирования. Повтор того же тела возвращает исходный результат и цену. Другое тело с тем же ключом даёт 409. После прерванной попытки нужен новый ключ. При отсутствии заголовка сервер генерирует UUID; повтор независимого HTTP-запроса считается новым намерением.

Ошибки имеют вид `{"error":{"code":"FLIGHT_RESOURCE_EXHAUSTED","message":"Not enough available seats"}}`. Некорректные REST-поля дают 422; gRPC INVALID_ARGUMENT — 400, NOT_FOUND — 404, RESOURCE_EXHAUSTED/ALREADY_EXISTS/FAILED_PRECONDITION — 409, UNAVAILABLE — 503, DEADLINE_EXCEEDED — 504. OPEN возвращает `503 FLIGHT_CIRCUIT_OPEN` без вызова Flight Service.

## Транзакции и восстановление

Flight Service сначала берёт транзакционный advisory lock по `booking_id`, затем `SELECT FOR UPDATE` строки рейса. Advisory lock защищает также ещё не существующую резервацию и гонку Reserve/Release. Проверка статуса, доступности и цены, уменьшение остатков, INSERT резервации и новая версия кеша выполняются в одной SQL-транзакции. Повторный Reserve с тем же ID и параметрами возвращает существующую ACTIVE-резервацию. Другие параметры дают ALREADY_EXISTS. Повторный Release не увеличивает остаток повторно.

Booking Service сначала вызывает GetFlight, затем сохраняет техническую команду PREPARING в `booking_attempts`. Под блокировкой команды выполняются ReserveSeats, INSERT Booking и переход команды в COMMITTED. Цена проверяется Flight Service под блокировкой: если она изменилась после GetFlight, операция отклоняется до списания мест. Цена подтверждённого бронирования больше не зависит от UpdateFlight.

У двух PostgreSQL нет общей транзакции. Поэтому при потере связи между коммитами возможен временный резерв без Booking; я устраняю его устойчивой компенсацией. При ошибке команда переходит в ABORTING, вызывается ReleaseReservation. Если сервис или БД недоступны, фоновый worker повторяет компенсацию после восстановления. Зависшие PREPARING старше `RECOVERY_GRACE_SECONDS` также компенсируются. COMMITTED-команды никогда не компенсируются, в том числе при потере подтверждения SQL-коммита.

Release неизвестного booking_id создаёт постоянный fence в `reservation_cancellations`. Поздний Reserve с этим ID отклоняется: медленный исходный RPC не может занять места после компенсации. При отмене существующего Booking сначала сохраняется `cancellation_jobs`; после успешного Release статус становится CANCELLED, а задание удаляется. Если ответ отмены был 503/504, намерение уже сохранено и будет завершено worker. Окончательный статус можно получить через GET Booking.

## Кеш и Sentinel

Ключи: `flight:{id}:{revision}` и `search:{origin}:{destination}:{date|all}:{revision}`. Любая успешная мутация ReserveSeats, ReleaseReservation или UpdateFlight меняет UUID `cache_revision` в той же транзакции. Старая версия удаляется через SCAN/UNLINK; все записи также имеют TTL. Если инвалидация не дошла до Redis, старое пространство ключей уже недоступно новым чтениям.

Каждое чтение получает небольшой SQL-снимок версии. При miss Flight и Search читают данные в той же REPEATABLE READ-транзакции, затем записывают protobuf в Redis. Это защищает от позднего заполнения кеша старым снимком и от устаревших данных на реплике после failover. Цена такой гарантии — один SQL-запрос версии даже при hit и глобальная смена пространства кеша при любой мутации. Для небольшого учебного каталога я выбрал простую проверяемую согласованность. Недоступность Redis приводит к чтению PostgreSQL; недоступность Flight DB возвращает UNAVAILABLE.

Sentinel работает с кворумом 2 из 3, `down-after-milliseconds=2000`. Redis-ноды объявляют стабильные DNS-имена через `replica-announce-ip`, Sentinel — через `sentinel announce-ip`; IP и DNS не смешиваются, что важно для повторных переключений. redis-py обнаруживает текущий мастер через Sentinel, чтения идут с мастера. Redis хранит только восстанавливаемый кеш; данные бронирований и мест находятся в PostgreSQL. Конфигурация выдерживает остановку одного Redis-мастера и одного Sentinel при сохранении кворума; размещение всего Compose на одном компьютере не защищает от отказа этого компьютера.

## Retry и Circuit Breaker

Оба механизма находятся в `ResilienceInterceptor`, а не в бизнес-операциях. Для одного логического RPC в CLOSED выполняется максимум три физические попытки с отдельным deadline. По умолчанию паузы перед второй и третьей попытками равны 100 и 200 мс. Ошибки других кодов не повторяются. Автоматические retry самого gRPC отключены.

Breaker учитывает окончательно неуспешные логические RPC с UNAVAILABLE/DEADLINE_EXCEEDED в скользящем окне. По умолчанию 5 ошибок за 30 секунд открывают цепь на 10 секунд. HALF_OPEN разрешает ровно один физический RPC, остальные запросы сразу получают 503. Успех, включая полученную бизнес-ошибку доступного сервиса, закрывает цепь. Транзиентный сбой пробного RPC открывает её снова. Блокировка и номер поколения защищают переходы от конкурентных запросов и поздних ответов старого поколения.

| Переменная | По умолчанию |
| --- | --- |
| `GRPC_API_KEY` | Локальный ключ из Compose, одинаковый для сервисов |
| `GRPC_TIMEOUT_SECONDS` | 1 |
| `RETRY_BACKOFF_SECONDS` | 0.1 |
| `CB_FAILURE_THRESHOLD` | 5 |
| `CB_WINDOW_SECONDS` | 30 |
| `CB_RESET_SECONDS` | 10 |
| `CACHE_TTL_SECONDS` | 300, допустимо 300–600 |
| `RECOVERY_INTERVAL_SECONDS` | 2 |
| `RECOVERY_GRACE_SECONDS` | 30 |

Логи в JSON содержат `cache_hit`, `cache_miss`, `cache_invalidated`, `cache_unavailable`, `grpc_retry`, `circuit_transition`, `compensation_completed`, `recovery_pending`, `booking_cancelled`. Ключ API и персональные данные пассажиров в логи не записываются.

## Генерация и разработка

```bash
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python requirements.txt
make generate
make lint
```

`.proto` — исходный gRPC-контракт. `grpc_tools.protoc` создаёт сообщения, type stubs и клиент/сервер в `generated/`; при Docker-сборке генерация выполняется автоматически. Сгенерированные файлы исключены из Git, воспроизводимость проверяет тест. Все зависимости, в том числе платформенные, зафиксированы в `requirements.txt`. Для обновления: `uv pip compile --universal requirements.in --output-file requirements.txt --python 3.12`.

В `scripts/grpc_call.py` находится клиент для ручных вызовов всех RPC. Семантика основана на официальной документации [gRPC status codes](https://grpc.io/docs/guides/status-codes/), [interceptors](https://grpc.io/docs/guides/interceptors/), [Redis Sentinel](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/) и [redis-py](https://redis.io/docs/latest/develop/clients/redis-py/connect/).
