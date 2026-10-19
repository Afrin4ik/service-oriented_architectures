import hashlib
import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import jwt
import pytest
from openapi_spec_validator import validate
from sqlalchemy import func, select, text, update

from app.config import settings
from app.contract import SPEC
from app.db import SessionLocal
from app.models import Order, Product, PromoCode, RefreshSession, User, UserOperation


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error_code"] == code, response.text
    return response.json()


def db_product(id):
    with SessionLocal() as db:
        product = db.get(Product, UUID(id))
        return {"stock": product.stock, "price": product.price, "status": product.status}


def uses(id):
    with SessionLocal() as db:
        return db.get(PromoCode, UUID(id)).current_uses


def expire_rate(user_id):
    with SessionLocal.begin() as db:
        db.execute(
            update(UserOperation)
            .where(UserOperation.user_id == UUID(user_id))
            .values(created_at=datetime.now(UTC) - timedelta(minutes=5))
        )


def test_openapi_and_reproducible_generation(client):
    validate(SPEC)
    assert client.get("/openapi.json").json() == SPEC
    root = Path(__file__).resolve().parents[1]
    outputs = [root / "generated/models.py", root / "generated/routes.py"]
    before = [hashlib.sha256(file.read_bytes()).hexdigest() for file in outputs]
    subprocess.run([sys.executable, "-m", "scripts.generate"], cwd=root, check=True)
    assert before == [hashlib.sha256(file.read_bytes()).hexdigest() for file in outputs]
    assert client.get("/health").json() == {"status": "ok"}


def test_registration_password_hash_roles_and_login(client):
    credentials = {"username": "Student", "password": "Password123"}
    response = client.post("/auth/register", json=credentials)
    assert response.status_code == 201, response.text
    assert response.json()["role"] == "USER"
    assert "password" not in response.json()
    with SessionLocal() as db:
        user = db.get(User, UUID(response.json()["id"]))
        assert user.password_hash.startswith("$argon2id$")
    error(client.post("/auth/register", json=credentials), 409, "USERNAME_TAKEN")
    error(
        client.post("/auth/register", json={**credentials, "role": "ADMIN"}),
        400,
        "VALIDATION_ERROR",
    )
    error(
        client.post("/auth/login", json={**credentials, "password": "WrongPass123"}),
        401,
        "INVALID_CREDENTIALS",
    )
    error(
        client.post("/auth/login", json={**credentials, "username": "unknown"}),
        401,
        "INVALID_CREDENTIALS",
    )
    pair = client.post("/auth/login", json=credentials).json()
    for name, lifetime, kind in [
        ("access_token", 1200, "access"),
        ("refresh_token", 14 * 86400, "refresh"),
    ]:
        claims = jwt.decode(
            pair[name], settings.jwt_secret, algorithms=["HS256"], audience=settings.jwt_audience
        )
        assert claims["role"] == "USER" and claims["type"] == kind
        assert claims["exp"] - claims["iat"] == lifetime


def test_access_refresh_expiry_rotation(client, buyer):
    error(client.get("/products"), 401, "TOKEN_INVALID")
    error(client.get("/products", headers={"Authorization": "Bearer junk"}), 401, "TOKEN_INVALID")
    error(
        client.get("/products", headers={"Authorization": "Bearer " + buyer["refresh_token"]}),
        401,
        "TOKEN_INVALID",
    )
    expired = jwt.decode(
        buyer["access_token"],
        settings.jwt_secret,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
    )
    expired["exp"] = int(datetime.now(UTC).timestamp()) - 1
    token = jwt.encode(expired, settings.jwt_secret, algorithm="HS256")
    error(
        client.get("/products", headers={"Authorization": "Bearer " + token}), 401, "TOKEN_EXPIRED"
    )
    error(
        client.post("/auth/refresh", json={"refresh_token": buyer["access_token"]}),
        401,
        "REFRESH_TOKEN_INVALID",
    )
    error(
        client.post("/auth/refresh", json={"refresh_token": "invalid"}),
        401,
        "REFRESH_TOKEN_INVALID",
    )
    response = client.post("/auth/refresh", json={"refresh_token": buyer["refresh_token"]})
    assert response.status_code == 200
    assert response.json()["refresh_token"] != buyer["refresh_token"]
    error(
        client.post("/auth/refresh", json={"refresh_token": buyer["refresh_token"]}),
        401,
        "REFRESH_TOKEN_INVALID",
    )
    assert (
        client.get(
            "/products", headers={"Authorization": "Bearer " + response.json()["access_token"]}
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(RefreshSession).where(RefreshSession.revoked)
            )
            == 1
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("name", "x" * 256),
        ("description", "x" * 4001),
        ("price", 0),
        ("price", -1),
        ("price", 0.001),
        ("price", "12.00"),
        ("price", True),
        ("stock", -1),
        ("stock", 1.5),
        ("stock", True),
        ("category", ""),
        ("category", "x" * 101),
        ("status", "WRONG"),
        ("name", None),
    ],
)
def test_product_contract_validation(client, seller, field, value):
    payload = {
        "name": "name",
        "price": 10,
        "stock": 1,
        "category": "cat",
        "status": "ACTIVE",
        field: value,
    }
    result = error(
        client.post("/products", headers=seller["headers"], json=payload), 400, "VALIDATION_ERROR"
    )
    assert any(field in violation["field"] for violation in result["details"]["violations"])
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Product)) == 0


def test_product_boundaries_pagination_filtering_and_soft_delete(client, seller, buyer, product):
    first = product(name="x" * 255, description="x" * 4000, price=0.01, stock=0, category="x" * 100)
    second = product(category="Exact", status="INACTIVE", price=9999999999.99)
    product(category="exact")
    headers = buyer["headers"]
    page = client.get("/products", headers=headers).json()
    assert page["totalElements"] == 3 and page["page"] == 0 and page["size"] == 20
    result = client.get("/products", headers=headers, params={"size": 1, "page": 1}).json()
    assert result["items"][0]["id"] == second["id"] and result["totalElements"] == 3
    assert client.get("/products", headers=headers, params={"page": 9}).json()["items"] == []
    result = client.get(
        "/products", headers=headers, params={"status": "INACTIVE", "category": "Exact"}
    ).json()
    assert result["totalElements"] == 1
    assert client.delete("/products/" + first["id"], headers=seller["headers"]).status_code == 204
    assert client.delete("/products/" + first["id"], headers=seller["headers"]).status_code == 204
    assert db_product(first["id"])["status"] == "ARCHIVED"
    assert client.get("/products/" + first["id"], headers=headers).json()["status"] == "ARCHIVED"


def test_role_matrix_and_product_ownership(client, account, seller, buyer, admin, product, promo):
    item = product()
    other = account("SELLER")
    payload = {
        key: item[key] for key in ["name", "description", "price", "stock", "category", "status"]
    }
    for actor in [seller, other, buyer, admin]:
        assert client.get("/products", headers=actor["headers"]).status_code == 200
        assert client.get("/products/" + item["id"], headers=actor["headers"]).status_code == 200
    error(client.post("/products", headers=buyer["headers"], json=payload), 403, "ACCESS_DENIED")
    error(
        client.post(
            "/products", headers=seller["headers"], json={**payload, "seller_id": other["id"]}
        ),
        403,
        "ACCESS_DENIED",
    )
    assert (
        client.post(
            "/products", headers=admin["headers"], json={**payload, "seller_id": other["id"]}
        ).status_code
        == 201
    )
    for actor in [buyer, other]:
        error(
            client.put("/products/" + item["id"], headers=actor["headers"], json=payload),
            403,
            "ACCESS_DENIED",
        )
        error(
            client.delete("/products/" + item["id"], headers=actor["headers"]), 403, "ACCESS_DENIED"
        )
    assert (
        client.put(
            "/products/" + item["id"], headers=admin["headers"], json={**payload, "price": 120}
        ).status_code
        == 200
    )
    error(
        client.post(
            "/orders",
            headers=seller["headers"],
            json={"items": [{"product_id": item["id"], "quantity": 1}]},
        ),
        403,
        "ACCESS_DENIED",
    )
    code = promo()
    promo_payload = {key: value for key, value in code.items() if key not in {"id", "current_uses"}}
    error(
        client.post("/promo-codes", headers=buyer["headers"], json=promo_payload),
        403,
        "ACCESS_DENIED",
    )
    error(
        client.post("/promo-codes", headers=admin["headers"], json=promo_payload),
        409,
        "PROMO_CODE_EXISTS",
    )


def test_invalid_json_paths_queries_unknown_fields(client, seller, buyer, product):
    item = product()
    for params in [{"page": -1}, {"size": 0}, {"size": 101}, {"status": "bad"}, {"size": "oops"}]:
        error(
            client.get("/products", headers=buyer["headers"], params=params),
            400,
            "VALIDATION_ERROR",
        )
    error(client.get("/products/bad-uuid", headers=buyer["headers"]), 400, "VALIDATION_ERROR")
    error(
        client.get("/products/" + str(uuid4()), headers=buyer["headers"]), 404, "PRODUCT_NOT_FOUND"
    )
    error(
        client.post(
            "/products",
            headers={**seller["headers"], "Content-Type": "application/json"},
            content='{ "name":',
        ),
        400,
        "VALIDATION_ERROR",
    )
    error(
        client.post("/products", headers=seller["headers"], content="{}"), 400, "VALIDATION_ERROR"
    )
    payload = {key: item[key] for key in ["name", "price", "stock", "category", "status"]}
    error(
        client.post("/products", headers=seller["headers"], json={**payload, "id": str(uuid4())}),
        400,
        "VALIDATION_ERROR",
    )
    error(client.get("/missing"), 404, "ROUTE_NOT_FOUND")
    error(
        client.patch("/products/" + item["id"], headers=seller["headers"]),
        405,
        "METHOD_NOT_ALLOWED",
    )


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"product_id": str(uuid4()), "quantity": 1}] * 51,
        [{"product_id": "bad", "quantity": 0}],
        [{"product_id": str(uuid4()), "quantity": 1000}],
    ],
)
def test_order_input_validation(client, buyer, items):
    error(
        client.post("/orders", headers=buyer["headers"], json={"items": items}),
        400,
        "VALIDATION_ERROR",
    )


def test_duplicate_positions_and_promo_pattern(client, buyer, product):
    item = product()
    items = [
        {"product_id": item["id"], "quantity": 1},
        {"product_id": item["id"].upper(), "quantity": 2},
    ]
    error(
        client.post("/orders", headers=buyer["headers"], json={"items": items}),
        400,
        "VALIDATION_ERROR",
    )
    error(
        client.post(
            "/orders", headers=buyer["headers"], json={"items": items[:1], "promo_code": "bad"}
        ),
        400,
        "VALIDATION_ERROR",
    )
    assert db_product(item["id"])["stock"] == 10


def test_order_snapshots_reservation_and_cancel(client, seller, buyer, product, order, promo):
    item = product(price=123.45)
    code = promo(min_order_amount=0)
    created = order(item, promo_code=code["code"])
    assert Decimal(str(created["total_amount"])) == Decimal("222.21")
    assert Decimal(str(created["discount_amount"])) == Decimal("24.69")
    assert uses(code["id"]) == 1 and db_product(item["id"])["stock"] == 8
    payload = {key: item[key] for key in ["name", "price", "stock", "category", "status"]}
    payload.update(price=999, stock=8)
    response = client.put("/products/" + item["id"], headers=seller["headers"], json=payload)
    assert response.status_code == 200 and response.json()["updated_at"] > item["updated_at"]
    saved = client.get("/orders/" + created["id"], headers=buyer["headers"]).json()
    assert saved["items"][0]["price_at_order"] == 123.45
    assert saved["total_amount"] == created["total_amount"]
    assert (
        client.post("/orders/" + created["id"] + "/cancel", headers=buyer["headers"]).json()[
            "status"
        ]
        == "CANCELED"
    )
    assert uses(code["id"]) == 0 and db_product(item["id"])["stock"] == 10
    error(
        client.post("/orders/" + created["id"] + "/cancel", headers=buyer["headers"]),
        409,
        "INVALID_STATE_TRANSITION",
    )
    assert db_product(item["id"])["stock"] == 10


def test_failed_create_is_atomic_and_lists_all_shortages(client, buyer, product):
    first, second = product(stock=1), product(stock=0)
    items = [{"product_id": item["id"], "quantity": 2} for item in [first, second]]
    result = error(
        client.post("/orders", headers=buyer["headers"], json={"items": items}),
        409,
        "INSUFFICIENT_STOCK",
    )
    assert len(result["details"]["products"]) == 2
    assert db_product(first["id"])["stock"] == 1
    error(
        client.post(
            "/orders",
            headers=buyer["headers"],
            json={"items": [{"product_id": first["id"], "quantity": 1}], "promo_code": "NONE"},
        ),
        422,
        "PROMO_CODE_INVALID",
    )
    assert db_product(first["id"])["stock"] == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert db.scalar(select(func.count()).select_from(UserOperation)) == 0


@pytest.mark.parametrize("status", ["INACTIVE", "ARCHIVED"])
def test_inactive_or_missing_products(client, buyer, product, status):
    item = product(status=status)
    error(
        client.post(
            "/orders",
            headers=buyer["headers"],
            json={"items": [{"product_id": item["id"], "quantity": 1}]},
        ),
        409,
        "PRODUCT_INACTIVE",
    )
    error(
        client.post(
            "/orders",
            headers=buyer["headers"],
            json={"items": [{"product_id": str(uuid4()), "quantity": 1}]},
        ),
        404,
        "PRODUCT_NOT_FOUND",
    )


def test_rate_limit_order_priority_active_and_separate_update_limit(client, buyer, product, order):
    item = product()
    created = order(item)
    payload = {"items": [{"product_id": item["id"], "quantity": 1}]}
    result = client.post("/orders", headers=buyer["headers"], json=payload)
    error(result, 429, "ORDER_LIMIT_EXCEEDED")
    assert int(result.headers["Retry-After"]) > 0
    assert (
        client.put("/orders/" + created["id"], headers=buyer["headers"], json=payload).status_code
        == 200
    )
    error(
        client.put("/orders/" + created["id"], headers=buyer["headers"], json=payload),
        429,
        "ORDER_LIMIT_EXCEEDED",
    )
    expire_rate(buyer["id"])
    error(client.post("/orders", headers=buyer["headers"], json=payload), 409, "ORDER_HAS_ACTIVE")
    assert db_product(item["id"])["stock"] == 9


def test_update_returns_old_stock_removes_promo_and_rolls_back(
    client, buyer, product, promo, order
):
    item = product(stock=3)
    code = promo(min_order_amount=200)
    created = order(item, quantity=3, promo_code=code["code"])
    payload = {"items": [{"product_id": item["id"], "quantity": 2}]}
    response = client.put("/orders/" + created["id"], headers=buyer["headers"], json=payload)
    assert response.status_code == 200 and response.json()["total_amount"] == 180
    assert db_product(item["id"])["stock"] == 1 and uses(code["id"]) == 1
    expire_rate(buyer["id"])
    payload["items"][0]["quantity"] = 1
    response = client.put("/orders/" + created["id"], headers=buyer["headers"], json=payload)
    assert response.json()["promo_code_id"] is None and response.json()["discount_amount"] == 0
    assert uses(code["id"]) == 0 and db_product(item["id"])["stock"] == 2
    expire_rate(buyer["id"])
    payload["items"][0]["quantity"] = 4
    error(
        client.put("/orders/" + created["id"], headers=buyer["headers"], json=payload),
        409,
        "INSUFFICIENT_STOCK",
    )
    assert db_product(item["id"])["stock"] == 2
    assert (
        client.get("/orders/" + created["id"], headers=buyer["headers"]).json()["items"][0][
            "quantity"
        ]
        == 1
    )


def test_update_uses_new_prices_retains_last_promo_use_and_expired_promo_rolls_back(
    client, buyer, seller, product, promo, order
):
    item = product()
    code = promo(max_uses=1, min_order_amount=0)
    created = order(item, promo_code=code["code"])
    payload = {key: item[key] for key in ["name", "price", "stock", "category", "status"]}
    assert (
        client.put(
            "/products/" + item["id"],
            headers=seller["headers"],
            json={**payload, "price": 50, "stock": 8},
        ).status_code
        == 200
    )
    update_payload = {"items": [{"product_id": item["id"], "quantity": 3}]}
    response = client.put("/orders/" + created["id"], headers=buyer["headers"], json=update_payload)
    assert response.status_code == 200, response.text
    assert (
        response.json()["total_amount"] == 135
        and response.json()["items"][0]["price_at_order"] == 50
    )
    assert uses(code["id"]) == 1
    expire_rate(buyer["id"])
    with SessionLocal.begin() as db:
        db.execute(update(PromoCode).where(PromoCode.id == UUID(code["id"])).values(active=False))
    update_payload["items"][0]["quantity"] = 1
    error(
        client.put("/orders/" + created["id"], headers=buyer["headers"], json=update_payload),
        422,
        "PROMO_CODE_INVALID",
    )
    assert uses(code["id"]) == 1 and db_product(item["id"])["stock"] == 7


@pytest.mark.parametrize(
    "promo_changes,expected",
    [
        ({"discount_value": 70}, 70),
        ({"discount_value": 71}, 0),
        ({"discount_type": "FIXED_AMOUNT", "discount_value": 300}, 100),
        ({"discount_type": "FIXED_AMOUNT", "discount_value": 12.34}, 12.34),
    ],
)
def test_discount_rules(client, buyer, product, promo, order, promo_changes, expected):
    item = product()
    code = promo(min_order_amount=0, **promo_changes)
    created = order(item, quantity=1, promo_code=code["code"])
    assert created["discount_amount"] == expected
    assert Decimal(str(created["total_amount"])) == Decimal("100") - Decimal(str(expected))


@pytest.mark.parametrize("kind", ["inactive", "expired", "future", "exhausted", "minimum"])
def test_promo_failure_is_atomic(client, buyer, product, promo, kind):
    item = product()
    changes = {}
    now = datetime.now(UTC)
    if kind == "inactive":
        changes["active"] = False
    if kind == "expired":
        changes.update(
            valid_from=(now - timedelta(days=2)).isoformat(),
            valid_until=(now - timedelta(days=1)).isoformat(),
        )
    if kind == "future":
        changes.update(
            valid_from=(now + timedelta(days=1)).isoformat(),
            valid_until=(now + timedelta(days=2)).isoformat(),
        )
    if kind == "minimum":
        changes["min_order_amount"] = 200
    code = promo(**changes)
    if kind == "exhausted":
        with SessionLocal.begin() as db:
            db.execute(
                update(PromoCode)
                .where(PromoCode.id == UUID(code["id"]))
                .values(current_uses=code["max_uses"])
            )
    error(
        client.post(
            "/orders",
            headers=buyer["headers"],
            json={"items": [{"product_id": item["id"], "quantity": 1}], "promo_code": code["code"]},
        ),
        422,
        "PROMO_CODE_MIN_AMOUNT" if kind == "minimum" else "PROMO_CODE_INVALID",
    )
    assert db_product(item["id"])["stock"] == 10


def test_order_ownership_and_complete_state_machine(
    client, buyer, admin, seller, account, product, order
):
    item = product()
    created = order(item)
    endpoint = "/orders/" + created["id"]
    other = account()
    payload = {"items": [{"product_id": item["id"], "quantity": 1}]}
    for method, suffix, kwargs in [
        ("get", "", {}),
        ("put", "", {"json": payload}),
        ("post", "/cancel", {}),
    ]:
        error(
            getattr(client, method)(endpoint + suffix, headers=other["headers"], **kwargs),
            403,
            "ORDER_OWNERSHIP_VIOLATION",
        )
        error(
            getattr(client, method)(endpoint + suffix, headers=seller["headers"], **kwargs),
            403,
            "ACCESS_DENIED",
        )
    error(client.get("/orders/" + str(uuid4()), headers=buyer["headers"]), 404, "ORDER_NOT_FOUND")
    assert client.get(endpoint, headers=admin["headers"]).status_code == 200
    assert client.put(endpoint, headers=admin["headers"], json=payload).status_code == 200
    error(
        client.post(
            endpoint + "/status", headers=buyer["headers"], json={"status": "PAYMENT_PENDING"}
        ),
        403,
        "ACCESS_DENIED",
    )
    error(
        client.post(endpoint + "/status", headers=admin["headers"], json={"status": "PAID"}),
        409,
        "INVALID_STATE_TRANSITION",
    )
    for status in ["PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"]:
        assert (
            client.post(
                endpoint + "/status", headers=admin["headers"], json={"status": status}
            ).json()["status"]
            == status
        )
        error(
            client.put(endpoint, headers=buyer["headers"], json=payload),
            409,
            "INVALID_STATE_TRANSITION",
        )
    error(
        client.post(endpoint + "/cancel", headers=buyer["headers"]), 409, "INVALID_STATE_TRANSITION"
    )
    error(
        client.post(endpoint + "/status", headers=admin["headers"], json={"status": "SHIPPED"}),
        409,
        "INVALID_STATE_TRANSITION",
    )


def test_pending_cancellation_by_admin_restores_stock_and_promo(
    client, buyer, admin, product, promo, order
):
    item = product()
    code = promo()
    created = order(item, promo_code=code["code"])
    endpoint = "/orders/" + created["id"]
    assert (
        client.post(
            endpoint + "/status", headers=admin["headers"], json={"status": "PAYMENT_PENDING"}
        ).status_code
        == 200
    )
    assert (
        client.post(endpoint + "/cancel", headers=admin["headers"]).json()["status"] == "CANCELED"
    )
    assert db_product(item["id"])["stock"] == 10 and uses(code["id"]) == 0


def parallel(actions):
    barrier = Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=10)
        return action()

    with ThreadPoolExecutor(max_workers=len(actions)) as executor:
        return list(executor.map(run, actions))


def test_concurrent_last_item_cannot_be_oversold(client, account, product):
    item = product(stock=1)
    buyers = [account(), account()]
    payload = {"items": [{"product_id": item["id"], "quantity": 1}]}
    responses = parallel(
        [
            lambda actor=actor: client.post("/orders", headers=actor["headers"], json=payload)
            for actor in buyers
        ]
    )
    assert sorted(result.status_code for result in responses) == [201, 409]
    error(
        next(result for result in responses if result.status_code == 409), 409, "INSUFFICIENT_STOCK"
    )
    assert db_product(item["id"])["stock"] == 0


def test_concurrent_orders_of_same_user_respect_rate_limit(client, buyer, product):
    item = product()
    payload = {"items": [{"product_id": item["id"], "quantity": 1}]}
    responses = parallel(
        [lambda: client.post("/orders", headers=buyer["headers"], json=payload) for _ in range(2)]
    )
    assert sorted(result.status_code for result in responses) == [201, 429]
    assert db_product(item["id"])["stock"] == 9


def test_concurrent_promo_last_use_and_refresh_rotation(client, account, buyer, product, promo):
    items = [product(), product()]
    buyers = [account(), account()]
    code = promo(max_uses=1)
    responses = parallel(
        [
            lambda item=item, actor=actor: client.post(
                "/orders",
                headers=actor["headers"],
                json={
                    "items": [{"product_id": item["id"], "quantity": 1}],
                    "promo_code": code["code"],
                },
            )
            for item, actor in zip(items, buyers, strict=True)
        ]
    )
    assert sorted(result.status_code for result in responses) == [201, 422]
    assert uses(code["id"]) == 1
    assert sorted(db_product(item["id"])["stock"] for item in items) == [9, 10]
    responses = parallel(
        [
            lambda: client.post("/auth/refresh", json={"refresh_token": buyer["refresh_token"]})
            for _ in range(2)
        ]
    )
    assert sorted(result.status_code for result in responses) == [200, 401]


def test_json_logging_request_id_and_secrets(client, buyer):
    class Capture(logging.Handler):
        def emit(self, record):
            records.append(json.loads(record.getMessage()))

    records = []
    logger = logging.getLogger("marketplace.requests")
    handler = Capture()
    logger.addHandler(handler)
    try:
        response = client.post(
            "/auth/login", json={"username": buyer["username"], "password": buyer["password"]}
        )
        client.post("/auth/refresh", json={"refresh_token": buyer["refresh_token"]})
        client.get("/products", headers=buyer["headers"])
        client.request(
            "DELETE",
            "/products/" + str(uuid4()),
            headers=buyer["headers"],
            json={"note": "attempt", "nested": {"password": "dont-log-this"}},
        )
        client.post(
            "/auth/login", content='{"password":NaN}', headers={"Content-Type": "application/json"}
        )
    finally:
        logger.removeHandler(handler)
    assert records[0]["request_id"] == response.headers["X-Request-Id"]
    assert records[0]["request_body"]["password"] == "***"
    assert records[1]["request_body"]["refresh_token"] == "***"
    assert records[2]["user_id"] == buyer["id"]
    assert records[3]["status_code"] == 403
    assert records[3]["request_body"] == {"note": "attempt", "nested": {"password": "***"}}
    assert records[4]["request_body"] == "[invalid JSON]"
    assert buyer["password"] not in json.dumps(records)
    assert buyer["refresh_token"] not in json.dumps(records)
    assert "dont-log-this" not in json.dumps(records)
    required = {
        "request_id",
        "method",
        "endpoint",
        "status_code",
        "duration_ms",
        "user_id",
        "timestamp",
    }
    assert all(required <= record.keys() for record in records)


def test_database_migrations_indexes_and_triggers():
    with SessionLocal() as db:
        assert db.scalar(text("SELECT count(*) FROM flyway_schema_history WHERE success")) == 1
        assert (
            db.scalar(
                text("SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_products_status'")
            )
            == 1
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM information_schema.triggers WHERE trigger_name IN ('products_touch_updated_at', 'orders_touch_updated_at')"
                )
            )
            == 2
        )
