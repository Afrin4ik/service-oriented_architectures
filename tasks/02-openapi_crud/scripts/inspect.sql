\pset pager off
SELECT id, username, role, created_at FROM users ORDER BY created_at;
SELECT id, name, price, stock, status, seller_id, created_at, updated_at FROM products ORDER BY created_at;
SELECT id, user_id, status, promo_code_id, total_amount, discount_amount FROM orders ORDER BY created_at;
SELECT order_id, product_id, quantity, price_at_order FROM order_items ORDER BY order_id, product_id;
SELECT code, discount_type, discount_value, min_order_amount, current_uses, max_uses, active FROM promo_codes ORDER BY code;
SELECT user_id, operation_type, created_at FROM user_operations ORDER BY created_at;
SELECT installed_rank, version, description, success FROM flyway_schema_history;
SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN ('products', 'orders', 'user_operations');
