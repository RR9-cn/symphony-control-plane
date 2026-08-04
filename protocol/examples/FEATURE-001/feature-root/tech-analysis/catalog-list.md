# 商品列表技术分析

查询限定 `merchant_id`，状态为可选过滤条件；使用 `(updated_at, id)` 组合游标维持稳定排序。
