from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import auth, orders, products
from app.db import SessionLocal
from app.errors import AppError
from app.models import User

HANDLERS = {
    function.__name__: function
    for function in [
        auth.register,
        auth.login,
        auth.refresh,
        products.create_product,
        products.get_product,
        products.list_products,
        products.update_product,
        products.delete_product,
        products.create_promo,
        orders.create_order,
        orders.get_order,
        orders.update_order,
        orders.cancel_order,
        orders.transition_order,
    ]
}


def execute(operation, request, claims, *, response_type=None, **kwargs):
    try:
        with SessionLocal.begin() as db:
            if operation == "health":
                db.execute(text("SELECT 1"))
                result = {"status": "ok"}
            else:
                actor = None
                if claims is not None:
                    actor = db.get(User, UUID(claims["sub"]))
                    if actor is None or actor.role != claims["role"]:
                        request.state.user_id = None
                        raise AppError(
                            401, "TOKEN_INVALID", "Token user or role is no longer valid"
                        )
                result = HANDLERS[operation](db, actor, **kwargs)
            db.flush()
            response = response_type.model_validate(result) if response_type else None
        return response
    except IntegrityError as exc:
        constraint = getattr(exc.orig.diag, "constraint_name", None)
        conflicts = {
            "users_username_key": ("USERNAME_TAKEN", "Username is already registered"),
            "promo_codes_code_key": ("PROMO_CODE_EXISTS", "Promo code already exists"),
            "uq_orders_active_user": ("ORDER_HAS_ACTIVE", "User already has an active order"),
        }
        if constraint in conflicts:
            code, message = conflicts[constraint]
            raise AppError(409, code, message) from exc
        raise
