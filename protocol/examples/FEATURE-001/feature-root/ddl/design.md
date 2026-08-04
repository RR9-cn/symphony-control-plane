# DDL 设计

复用 `product(id, merchant_id, status, updated_at)`，新增 `(merchant_id, status, updated_at, id)` 查询索引。
