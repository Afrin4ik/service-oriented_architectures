import os
from uuid import uuid4

import httpx


def main():
    with httpx.Client(
        base_url=os.getenv("BOOKING_URL", "http://127.0.0.1:8000"), timeout=10
    ) as client:
        flights = client.get("/flights", params={"origin": "SVO", "destination": "LED"})
        flights.raise_for_status()
        flight = next(item for item in flights.json() if item["available_seats"] >= 2)
        user_id, booking_id = str(uuid4()), str(uuid4())
        payload = {
            "user_id": user_id,
            "flight_id": flight["id"],
            "passenger_name": "Иван Петров",
            "passenger_email": "ivan@example.com",
            "seat_count": 2,
        }
        headers = {"Idempotency-Key": booking_id}
        response = client.post("/bookings", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        booking = response.json()
        assert client.post("/bookings", json=payload, headers=headers).json() == booking
        assert (
            client.get(f"/flights/{flight['id']}").json()["available_seats"]
            == flight["available_seats"] - 2
        )
        assert client.get(f"/bookings/{booking_id}").json() == booking
        assert client.get("/bookings", params={"user_id": user_id}).json() == [booking]
        for _ in range(2):
            assert client.post(f"/bookings/{booking_id}/cancel").json()["status"] == "CANCELLED"
        assert (
            client.get(f"/flights/{flight['id']}").json()["available_seats"]
            == flight["available_seats"]
        )
        assert client.get(f"/flights/{uuid4()}").status_code == 404
        assert client.post("/bookings", json={**payload, "seat_count": 10000}).status_code == 409
        assert client.post("/bookings", json={**payload, "seat_count": 0}).status_code == 422
        print(
            f"E2E passed: booking={booking_id}, total_price={booking['total_price']}, seats restored"
        )


if __name__ == "__main__":
    main()
