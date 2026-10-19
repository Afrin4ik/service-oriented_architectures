import json
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-characters-for-jwt")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://marketplace:marketplace@localhost:5433/marketplace_test"
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import passwords, token_pair
from app.contract import SPEC, ContractValidator, decimals
from app.db import SessionLocal, engine
from app.main import app
from app.models import User


class ContractClient(TestClient):
    def request(self, method, url, *args, **kwargs):
        response = super().request(method, url, *args, **kwargs)
        path = response.request.url.path
        for template, operations in SPEC["paths"].items():
            pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template))
            if not re.fullmatch(pattern, path) or method.lower() not in operations:
                continue
            operation = operations[method.lower()]
            responses = operation["responses"]
            contract = responses.get(str(response.status_code), responses.get("default"))
            assert contract is not None, response.text
            if "$ref" in contract:
                contract = SPEC["components"]["responses"][contract["$ref"].rsplit("/", 1)[1]]
            schema = contract.get("content", {}).get("application/json", {}).get("schema")
            if schema:
                payload = json.loads(response.text, parse_float=Decimal)
                ContractValidator(decimals({**schema, "components": SPEC["components"]})).validate(
                    payload
                )
            else:
                assert not response.content
            break
        assert UUID(response.headers["X-Request-Id"])
        return response


@pytest.fixture(autouse=True)
def clean_database():
    assert engine.url.database.endswith("_test"), "Tests require a separate *_test database"
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE refresh_sessions, user_operations, order_items, orders, promo_codes, products, users CASCADE"
            )
        )
    yield


@pytest.fixture
def client():
    with ContractClient(app) as value:
        yield value


@pytest.fixture
def account(client):
    def create(role="USER"):
        username = "test_" + uuid4().hex[:12]
        credentials = {"username": username, "password": "Password123"}
        if role == "ADMIN":
            with SessionLocal.begin() as db:
                user = User(
                    username=username,
                    password_hash=passwords.hash(credentials["password"]),
                    role=role,
                )
                db.add(user)
                db.flush()
                id = str(user.id)
                pair = token_pair(db, user)
        else:
            response = client.post("/auth/register", json={**credentials, "role": role})
            assert response.status_code == 201, response.text
            id = response.json()["id"]
            response = client.post("/auth/login", json=credentials)
            assert response.status_code == 200, response.text
            pair = response.json()
        return {
            "id": id,
            "headers": {"Authorization": "Bearer " + pair["access_token"]},
            **credentials,
            **pair,
        }

    return create


@pytest.fixture
def seller(account):
    return account("SELLER")


@pytest.fixture
def buyer(account):
    return account()


@pytest.fixture
def admin(account):
    return account("ADMIN")


@pytest.fixture
def product(client, seller):
    def create(**changes):
        payload = {
            "name": "Keyboard",
            "price": 100,
            "stock": 10,
            "category": "electronics",
            "status": "ACTIVE",
            **changes,
        }
        response = client.post("/products", headers=seller["headers"], json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return create


@pytest.fixture
def promo(client, seller):
    def create(**changes):
        now = datetime.now(UTC)
        payload = {
            "code": "SALE_" + uuid4().hex[:8].upper(),
            "discount_type": "PERCENTAGE",
            "discount_value": 10,
            "min_order_amount": 100,
            "max_uses": 10,
            "valid_from": (now - timedelta(days=1)).isoformat(),
            "valid_until": (now + timedelta(days=1)).isoformat(),
            **changes,
        }
        response = client.post("/promo-codes", headers=seller["headers"], json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return create


@pytest.fixture
def order(client, buyer, product):
    def create(item=None, quantity=2, **changes):
        item = item or product()
        response = client.post(
            "/orders",
            headers=buyer["headers"],
            json={
                "items": [{"product_id": item["id"], "quantity": quantity}],
                **changes,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    return create
