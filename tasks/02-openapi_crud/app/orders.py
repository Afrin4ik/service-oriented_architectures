import math
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from app.config import settings
from app.errors import AppError, invalid
from app.models import Order, OrderItem, Product, PromoCode, User, UserOperation

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("9999999999.99")
NEXT_STATUS = {
    "CREATED": "PAYMENT_PENDING",
    "PAYMENT_PENDING": "PAID",
    "PAID": "SHIPPED",
    "SHIPPED": "COMPLETED",
}


def lock_user(db, actor):
    db.scalar(select(User).where(User.id == actor.id).with_for_update())


def check_rate(db, actor, operation):
    last = db.scalar(
        select(UserOperation.created_at)
        .where(UserOperation.user_id == actor.id, UserOperation.operation_type == operation)
        .order_by(UserOperation.created_at.desc())
        .limit(1)
    )
    if last is not None:
        remaining = (
            float(settings.order_interval_minutes * 60) - (datetime.now(UTC) - last).total_seconds()
        )
        if remaining > 0:
            retry = math.ceil(remaining)
            raise AppError(
                429,
                "ORDER_LIMIT_EXCEEDED",
                "Order operation rate limit exceeded",
                {
                    "operation_type": operation,
                    "retry_after_seconds": retry,
                },
                headers={"Retry-After": str(retry)},
            )


def find_order(db, actor, id, lock=False):
    statement = select(Order).where(Order.id == id)
    order = db.scalar(statement.with_for_update() if lock else statement)
    if order is None:
        raise AppError(404, "ORDER_NOT_FOUND", "Order not found", {"order_id": str(id)})
    if actor.role != "ADMIN" and order.user_id != actor.id:
        raise AppError(403, "ORDER_OWNERSHIP_VIOLATION", "Order belongs to another user")
    return order


def check_state(order, allowed):
    if order.status not in allowed:
        raise AppError(
            409,
            "INVALID_STATE_TRANSITION",
            "Operation is not allowed in the current state",
            {
                "status": order.status,
                "allowed_states": sorted(allowed),
            },
        )


def lock_products(db, ids):
    return {
        product.id: product
        for product in db.scalars(
            select(Product).where(Product.id.in_(ids)).order_by(Product.id).with_for_update()
        )
    }


def reserve(items, products):
    for item in items:
        product = products.get(item.product_id)
        if product is None:
            raise AppError(
                404, "PRODUCT_NOT_FOUND", "Product not found", {"product_id": str(item.product_id)}
            )
        if product.status != "ACTIVE":
            raise AppError(
                409, "PRODUCT_INACTIVE", "Product is not active", {"product_id": str(product.id)}
            )
    missing = [
        {
            "product_id": str(item.product_id),
            "requested_quantity": item.quantity,
            "available_stock": products[item.product_id].stock,
        }
        for item in items
        if products[item.product_id].stock < item.quantity
    ]
    if missing:
        raise AppError(
            409, "INSUFFICIENT_STOCK", "Insufficient product stock", {"products": missing}
        )
    snapshots = []
    for item in items:
        product = products[item.product_id]
        product.stock -= item.quantity
        snapshots.append(
            OrderItem(product_id=product.id, quantity=item.quantity, price_at_order=product.price)
        )
    return snapshots


def subtotal(items):
    amount = sum((item.price_at_order * item.quantity for item in items), Decimal("0"))
    if amount > MAX_AMOUNT:
        invalid("body.items", "Order subtotal exceeds 9999999999.99")
    return amount


def check_promo(promo, retained=False):
    now = datetime.now(UTC)
    if (
        promo is None
        or not promo.active
        or not promo.valid_from <= now <= promo.valid_until
        or promo.current_uses >= promo.max_uses + int(retained)
    ):
        raise AppError(
            422, "PROMO_CODE_INVALID", "Promo code is missing, inactive, expired or exhausted"
        )


def discount(promo, amount):
    if promo.discount_type == "PERCENTAGE":
        value = amount * promo.discount_value / 100 if promo.discount_value <= 70 else Decimal("0")
    else:
        value = min(promo.discount_value, amount)
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def create_order(db, actor, body):
    lock_user(db, actor)
    check_rate(db, actor, "CREATE_ORDER")
    active = db.scalar(
        select(Order.id)
        .where(Order.user_id == actor.id, Order.status.in_(["CREATED", "PAYMENT_PENDING"]))
        .limit(1)
    )
    if active is not None:
        raise AppError(
            409, "ORDER_HAS_ACTIVE", "User already has an active order", {"order_id": str(active)}
        )
    products = lock_products(db, [item.product_id for item in body.items])
    items = reserve(body.items, products)
    amount = subtotal(items)
    promo = None
    reduction = Decimal("0")
    if body.promo_code is not None:
        promo = db.scalar(
            select(PromoCode).where(PromoCode.code == body.promo_code).with_for_update()
        )
        check_promo(promo)
        if amount < promo.min_order_amount:
            raise AppError(
                422,
                "PROMO_CODE_MIN_AMOUNT",
                "Order subtotal is below the promo minimum",
                {
                    "min_order_amount": str(promo.min_order_amount),
                    "subtotal": str(amount),
                },
            )
        reduction = discount(promo, amount)
        promo.current_uses += 1
    order = Order(
        user_id=actor.id,
        status="CREATED",
        promo_code_id=promo.id if promo else None,
        total_amount=amount - reduction,
        discount_amount=reduction,
        items=items,
    )
    db.add(order)
    db.add(
        UserOperation(user_id=actor.id, operation_type="CREATE_ORDER", created_at=datetime.now(UTC))
    )
    return order


def get_order(db, actor, id):
    return find_order(db, actor, id)


def update_order(db, actor, id, body):
    lock_user(db, actor)
    order = find_order(db, actor, id, lock=True)
    check_state(order, {"CREATED"})
    check_rate(db, actor, "UPDATE_ORDER")
    product_ids = {item.product_id for item in order.items} | {
        item.product_id for item in body.items
    }
    products = lock_products(db, product_ids)
    for old in order.items:
        products[old.product_id].stock += old.quantity
    items = reserve(body.items, products)
    amount = subtotal(items)
    reduction = Decimal("0")
    if order.promo_code_id is not None:
        promo = db.scalar(
            select(PromoCode).where(PromoCode.id == order.promo_code_id).with_for_update()
        )
        check_promo(promo, retained=True)
        if amount < promo.min_order_amount:
            order.promo_code_id = None
            promo.current_uses -= 1
        else:
            reduction = discount(promo, amount)
    order.items.clear()
    db.flush()
    order.items = items
    order.total_amount = amount - reduction
    order.discount_amount = reduction
    order.updated_at = datetime.now(UTC)
    db.add(
        UserOperation(user_id=actor.id, operation_type="UPDATE_ORDER", created_at=datetime.now(UTC))
    )
    return order


def cancel_order(db, actor, id):
    lock_user(db, actor)
    order = find_order(db, actor, id, lock=True)
    check_state(order, {"CREATED", "PAYMENT_PENDING"})
    products = lock_products(db, [item.product_id for item in order.items])
    for item in order.items:
        products[item.product_id].stock += item.quantity
    if order.promo_code_id is not None:
        promo = db.scalar(
            select(PromoCode).where(PromoCode.id == order.promo_code_id).with_for_update()
        )
        promo.current_uses -= 1
    order.status = "CANCELED"
    return order


def transition_order(db, actor, id, body):
    lock_user(db, actor)
    order = find_order(db, actor, id, lock=True)
    if NEXT_STATUS.get(order.status) != body.status:
        raise AppError(
            409,
            "INVALID_STATE_TRANSITION",
            "Invalid order state transition",
            {
                "status": order.status,
                "requested_status": body.status,
                "next_status": NEXT_STATUS.get(order.status),
            },
        )
    order.status = body.status
    return order
