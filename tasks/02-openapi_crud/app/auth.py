import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select

from app.config import settings
from app.errors import AppError
from app.models import RefreshSession, User

passwords = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)
DUMMY_HASH = passwords.hash("unavailable-account-password")


def plaintext(value):
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def decode_token(token, expected):
    code = "REFRESH_TOKEN_INVALID" if expected == "refresh" else "TOKEN_INVALID"
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "role", "type", "exp", "iat", "nbf", "jti", "iss", "aud"]},
        )
        UUID(claims["sub"])
        UUID(claims["jti"])
        if claims["type"] != expected or claims["role"] not in {"USER", "SELLER", "ADMIN"}:
            raise ValueError("Wrong token type or role")
        return claims
    except jwt.ExpiredSignatureError as exc:
        expired_code = "TOKEN_EXPIRED" if expected == "access" else code
        raise AppError(
            401, expired_code, "Token expired", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AppError(401, code, "Invalid token", headers={"WWW-Authenticate": "Bearer"}) from exc


def require_roles(roles):
    def authenticate(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None:
            raise AppError(
                401,
                "TOKEN_INVALID",
                "Bearer access token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        claims = decode_token(credentials.credentials, "access")
        request.state.user_id = claims["sub"]
        if claims["role"] not in roles:
            raise AppError(403, "ACCESS_DENIED", "Role is not allowed for this operation")
        return claims

    return authenticate


def token_pair(db, user):
    now = datetime.now(UTC)
    common = {
        "sub": str(user.id),
        "role": user.role,
        "iat": now,
        "nbf": now,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    access = jwt.encode(
        {
            **common,
            "type": "access",
            "exp": now + timedelta(minutes=settings.access_minutes),
            "jti": str(uuid4()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    session_id = uuid4()
    expires = now + timedelta(days=settings.refresh_days)
    refresh = jwt.encode(
        {**common, "type": "refresh", "exp": expires, "jti": str(session_id)},
        settings.jwt_secret,
        algorithm="HS256",
    )
    db.add(
        RefreshSession(
            id=session_id,
            user_id=user.id,
            token_hash=hashlib.sha256(refresh.encode()).hexdigest(),
            expires_at=expires,
        )
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": settings.access_minutes * 60,
    }


def register(db, actor, body):
    user = User(
        username=body.username.lower(),
        password_hash=passwords.hash(plaintext(body.password)),
        role=body.role,
    )
    db.add(user)
    db.flush()
    return user


def login(db, actor, body):
    user = db.scalar(select(User).where(User.username == body.username.lower()))
    valid = passwords.verify(plaintext(body.password), user.password_hash if user else DUMMY_HASH)
    if not user or not valid:
        raise AppError(401, "INVALID_CREDENTIALS", "Invalid username or password")
    return token_pair(db, user)


def refresh(db, actor, body):
    token = plaintext(body.refresh_token)
    claims = decode_token(token, "refresh")
    session = db.scalar(
        select(RefreshSession)
        .where(
            RefreshSession.id == UUID(claims["jti"]),
            RefreshSession.token_hash == hashlib.sha256(token.encode()).hexdigest(),
        )
        .with_for_update()
    )
    user = db.get(User, UUID(claims["sub"]))
    if (
        session is None
        or session.revoked
        or session.expires_at <= datetime.now(UTC)
        or user is None
        or session.user_id != user.id
        or user.role != claims["role"]
    ):
        raise AppError(401, "REFRESH_TOKEN_INVALID", "Refresh token is invalid or already used")
    session.revoked = True
    return token_pair(db, user)
