import os

from sqlalchemy import select

from app.auth import passwords
from app.db import SessionLocal
from app.models import User


def main():
    username = os.environ.get("ADMIN_USERNAME", "admin").lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if len(password) < 8:
        raise ValueError("ADMIN_PASSWORD must contain at least 8 characters")
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            db.add(User(username=username, password_hash=passwords.hash(password), role="ADMIN"))
        elif user.role != "ADMIN":
            raise ValueError("ADMIN_USERNAME is already used by a non-admin account")


if __name__ == "__main__":
    main()
