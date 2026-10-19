from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    FetchedValue,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), server_onupdate=FetchedValue()
    )


class User(Identity, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(Enum("USER", "SELLER", "ADMIN", name="user_role"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Product(Identity, Timestamps, Base):
    __tablename__ = "products"
    __mapper_args__ = {"eager_defaults": True}
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(4000))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    stock: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        Enum("ACTIVE", "INACTIVE", "ARCHIVED", name="product_status")
    )
    seller_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class PromoCode(Identity, Base):
    __tablename__ = "promo_codes"
    code: Mapped[str] = mapped_column(String(20), unique=True)
    discount_type: Mapped[str] = mapped_column(
        Enum("PERCENTAGE", "FIXED_AMOUNT", name="discount_type")
    )
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    min_order_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    max_uses: Mapped[int] = mapped_column(Integer)
    current_uses: Mapped[int] = mapped_column(Integer, default=0)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Identity, Timestamps, Base):
    __tablename__ = "orders"
    __mapper_args__ = {"eager_defaults": True}
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(
        Enum(
            "CREATED",
            "PAYMENT_PENDING",
            "PAID",
            "SHIPPED",
            "COMPLETED",
            "CANCELED",
            name="order_status",
        )
    )
    promo_code_id: Mapped[UUID | None] = mapped_column(ForeignKey("promo_codes.id"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    items: Mapped[list["OrderItem"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="OrderItem.id"
    )


class OrderItem(Identity, Base):
    __tablename__ = "order_items"
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    price_at_order: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class UserOperation(Identity, Base):
    __tablename__ = "user_operations"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    operation_type: Mapped[str] = mapped_column(
        Enum("CREATE_ORDER", "UPDATE_ORDER", name="operation_type")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshSession(Identity, Base):
    __tablename__ = "refresh_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
