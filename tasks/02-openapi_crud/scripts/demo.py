import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx


def main():
    suffix = uuid4().hex[:8]
    base = os.environ.get("API_URL", "http://localhost:8000")
    with httpx.Client(base_url=base, timeout=20) as client:

        def call(method, path, actor=None, payload=None, status=200, code=None):
            headers = {"Authorization": "Bearer " + actor["access_token"]} if actor else {}
            response = client.request(method, path, headers=headers, json=payload)
            result = response.json() if response.content else {}
            assert response.status_code == status, (
                f"{method} {path}: {response.status_code} {result}"
            )
            if code:
                assert result["error_code"] == code, result
            display = {key: ("***" if "token" in key else value) for key, value in result.items()}
            print(f"{method} {path} -> {status} request_id={response.headers['X-Request-Id']}")
            print(display)
            return result

        def account(name, role):
            credentials = {"username": f"{name}_{suffix}", "password": "Password123"}
            call("POST", "/auth/register", payload={**credentials, "role": role}, status=201)
            return call("POST", "/auth/login", payload=credentials)

        seller = account("seller", "SELLER")
        buyer = account("buyer", "USER")
        other = account("other", "USER")
        admin = call(
            "POST",
            "/auth/login",
            payload={
                "username": os.environ.get("ADMIN_USERNAME", "admin"),
                "password": os.environ.get("ADMIN_PASSWORD", "AdminPass123"),
            },
        )
        product_payload = {
            "name": f"Keyboard {suffix}",
            "price": 100,
            "stock": 5,
            "category": "electronics",
            "status": "ACTIVE",
        }
        item = call("POST", "/products", seller, product_payload, status=201)
        call("POST", "/products", buyer, product_payload, status=403, code="ACCESS_DENIED")
        call(
            "POST",
            "/products",
            seller,
            {**product_payload, "price": 0},
            status=400,
            code="VALIDATION_ERROR",
        )
        now = datetime.now(UTC)
        promo = call(
            "POST",
            "/promo-codes",
            seller,
            {
                "code": f"DEMO_{suffix.upper()}",
                "discount_type": "PERCENTAGE",
                "discount_value": 10,
                "min_order_amount": 300,
                "max_uses": 10,
                "valid_from": (now - timedelta(days=1)).isoformat(),
                "valid_until": (now + timedelta(days=1)).isoformat(),
            },
            status=201,
        )
        payload = {
            "items": [{"product_id": item["id"], "quantity": 3}],
            "promo_code": promo["code"],
        }
        order = call("POST", "/orders", buyer, payload, status=201)
        assert order["total_amount"] == 270
        endpoint = "/orders/" + order["id"]
        call("POST", "/orders", buyer, payload, status=429, code="ORDER_LIMIT_EXCEEDED")
        call("GET", endpoint, other, status=403, code="ORDER_OWNERSHIP_VIOLATION")
        call("GET", endpoint, seller, status=403, code="ACCESS_DENIED")
        changed = {"items": [{"product_id": item["id"], "quantity": 2}]}
        updated = call("PUT", endpoint, buyer, changed)
        assert updated["promo_code_id"] is None and updated["discount_amount"] == 0
        call("PUT", endpoint, buyer, changed, status=429, code="ORDER_LIMIT_EXCEEDED")
        call(
            "PUT", "/products/" + item["id"], seller, {**product_payload, "price": 200, "stock": 3}
        )
        saved = call("GET", endpoint, buyer)
        assert saved["items"][0]["price_at_order"] == 100 and saved["total_amount"] == 200
        call("POST", endpoint + "/cancel", buyer)
        assert call("GET", "/products/" + item["id"], buyer)["stock"] == 5
        call("POST", endpoint + "/cancel", buyer, status=409, code="INVALID_STATE_TRANSITION")
        call(
            "POST",
            "/orders",
            other,
            {"items": [{"product_id": item["id"], "quantity": 6}]},
            status=409,
            code="INSUFFICIENT_STOCK",
        )
        call(
            "POST",
            "/orders",
            other,
            {"items": [{"product_id": item["id"], "quantity": 1}], "promo_code": promo["code"]},
            status=422,
            code="PROMO_CODE_MIN_AMOUNT",
        )
        order = call("POST", "/orders", other, {**changed, "promo_code": promo["code"]}, status=201)
        endpoint = "/orders/" + order["id"]
        call(
            "POST",
            endpoint + "/status",
            admin,
            {"status": "SHIPPED"},
            status=409,
            code="INVALID_STATE_TRANSITION",
        )
        for state in ["PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"]:
            call("POST", endpoint + "/status", admin, {"status": state})
        call("POST", endpoint + "/cancel", other, status=409, code="INVALID_STATE_TRANSITION")
        call("GET", "/products?page=0&size=1&status=ACTIVE&category=electronics", buyer)
        call("DELETE", "/products/" + item["id"], seller, status=204)
        assert call("GET", "/products/" + item["id"], buyer)["status"] == "ARCHIVED"
        pair = call("POST", "/auth/refresh", payload={"refresh_token": buyer["refresh_token"]})
        call(
            "POST",
            "/auth/refresh",
            payload={"refresh_token": buyer["refresh_token"]},
            status=401,
            code="REFRESH_TOKEN_INVALID",
        )
        call("GET", "/products", pair)
        print("E2E passed. Inspect persisted rows with: make inspect")


if __name__ == "__main__":
    main()
