# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 分批退货签发

- `POST /api/issuance/orders`：登记订单（`order_id`，可选 `profile` 或 `rows`），落整单一次退清目标。
- `GET /api/issuance/orders/{order_id}`：订单签发台账，逐批列出金额、运费与签发后累计。
- `POST /api/issuance/orders/{order_id}/preview`：按本批累计退货数量预览，不写库。
- `POST /api/issuance/orders/{order_id}/batches`：签发一批。`request_key` 相同的重试幂等返回，不重复记账。
- `GET /api/issuance/orders/{order_id}/batches`：与订单台账相同的批次清单。

金额单位均为整数分。每条批次响应交代 `amount`（本批）、`detail.issued_before`（批前累计）、
`cumulative_after`（批后累计）与订单实付的余额；累计退款超过实付返回 `409 CUMULATIVE_OVER_PAYMENT`。
