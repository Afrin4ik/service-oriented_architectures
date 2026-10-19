from sqlalchemy import func, select

from app.errors import AppError, invalid
from app.models import Product, PromoCode, User


def find_product(db, id, lock=False):
    query = select(Product).where(Product.id == id)
    product = db.scalar(query.with_for_update() if lock else query)
    if product is None:
        raise AppError(404, "PRODUCT_NOT_FOUND", "Product not found", {"product_id": str(id)})
    return product


def owned(product, actor):
    if actor.role != "ADMIN" and product.seller_id != actor.id:
        raise AppError(403, "ACCESS_DENIED", "Product belongs to another seller")


def create_product(db, actor, body):
    seller_id = body.seller_id or actor.id
    if actor.role != "ADMIN" and seller_id != actor.id:
        raise AppError(403, "ACCESS_DENIED", "Seller can create only owned products")
    seller = db.get(User, seller_id)
    if seller is None or seller.role not in {"SELLER", "ADMIN"}:
        invalid("body.seller_id", "Seller must be an existing SELLER or ADMIN")
    product = Product(**body.model_dump(exclude={"seller_id"}), seller_id=seller_id)
    db.add(product)
    return product


def get_product(db, actor, id):
    return find_product(db, id)


def list_products(db, actor, page, size, status, category):
    filters = []
    if status is not None:
        filters.append(Product.status == status)
    if category is not None:
        filters.append(Product.category == category)
    total = db.scalar(select(func.count()).select_from(Product).where(*filters))
    items = db.scalars(
        select(Product)
        .where(*filters)
        .order_by(Product.created_at, Product.id)
        .offset(page * size)
        .limit(size)
    ).all()
    return {"items": items, "totalElements": total, "page": page, "size": size}


def update_product(db, actor, id, body):
    product = find_product(db, id, lock=True)
    owned(product, actor)
    for key, value in body.model_dump().items():
        setattr(product, key, value)
    return product


def delete_product(db, actor, id):
    product = find_product(db, id, lock=True)
    owned(product, actor)
    product.status = "ARCHIVED"


def create_promo(db, actor, body):
    if body.valid_from.tzinfo is None or body.valid_until.tzinfo is None:
        invalid("body.valid_until", "Dates must contain a timezone")
    if body.valid_until <= body.valid_from:
        invalid("body.valid_until", "Must be later than valid_from")
    if body.discount_type == "PERCENTAGE" and body.discount_value > 100:
        invalid("body.discount_value", "Percentage must not exceed 100")
    promo = PromoCode(**body.model_dump())
    db.add(promo)
    return promo
