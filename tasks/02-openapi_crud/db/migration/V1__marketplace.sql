CREATE TYPE user_role AS ENUM ('USER', 'SELLER', 'ADMIN');
CREATE TYPE product_status AS ENUM ('ACTIVE', 'INACTIVE', 'ARCHIVED');
CREATE TYPE order_status AS ENUM ('CREATED', 'PAYMENT_PENDING', 'PAID', 'SHIPPED', 'COMPLETED', 'CANCELED');
CREATE TYPE discount_type AS ENUM ('PERCENTAGE', 'FIXED_AMOUNT');
CREATE TYPE operation_type AS ENUM ('CREATE_ORDER', 'UPDATE_ORDER');

CREATE TABLE users (
    id UUID PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role user_role NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE products (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL CHECK (length(name) >= 1),
    description VARCHAR(4000),
    price NUMERIC(12, 2) NOT NULL CHECK (price > 0),
    stock BIGINT NOT NULL CHECK (stock >= 0),
    category VARCHAR(100) NOT NULL CHECK (length(category) >= 1),
    status product_status NOT NULL,
    seller_id UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX idx_products_status ON products(status);
CREATE INDEX idx_products_category ON products(category);
CREATE INDEX idx_products_seller ON products(seller_id);

CREATE TABLE promo_codes (
    id UUID PRIMARY KEY,
    code VARCHAR(20) NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9_]{4,20}$'),
    discount_type discount_type NOT NULL,
    discount_value NUMERIC(12, 2) NOT NULL CHECK (discount_value > 0),
    min_order_amount NUMERIC(12, 2) NOT NULL CHECK (min_order_amount >= 0),
    max_uses INTEGER NOT NULL CHECK (max_uses > 0),
    current_uses INTEGER NOT NULL DEFAULT 0 CHECK (current_uses >= 0 AND current_uses <= max_uses),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL CHECK (valid_until > valid_from),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    CHECK (discount_type <> 'PERCENTAGE' OR discount_value <= 100)
);

CREATE TABLE orders (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    status order_status NOT NULL,
    promo_code_id UUID REFERENCES promo_codes(id),
    total_amount NUMERIC(12, 2) NOT NULL CHECK (total_amount >= 0),
    discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX uq_orders_active_user ON orders(user_id) WHERE status IN ('CREATED', 'PAYMENT_PENDING');
CREATE INDEX idx_orders_user ON orders(user_id);
CREATE INDEX idx_orders_promo ON orders(promo_code_id);

CREATE TABLE order_items (
    id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES orders(id),
    product_id UUID NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 999),
    price_at_order NUMERIC(12, 2) NOT NULL CHECK (price_at_order > 0),
    UNIQUE (order_id, product_id)
);
CREATE INDEX idx_order_items_product ON order_items(product_id);

CREATE TABLE user_operations (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    operation_type operation_type NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX idx_user_operations_latest ON user_operations(user_id, operation_type, created_at DESC);

CREATE TABLE refresh_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX idx_refresh_sessions_user ON refresh_sessions(user_id);

CREATE FUNCTION touch_updated_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER products_touch_updated_at BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER orders_touch_updated_at BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
