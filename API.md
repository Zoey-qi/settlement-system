# Settlement API Reference

结算单（settlement_records）相关 API，所有端点要求登录态（cookie 或 `X-Auth-Token` header）。

## 列表 / 汇总

### `GET /api/settlement-records`

查询结算单列表（leader + admin）。返回 `records[]`，每条含：

```json
{
  "id": 12,
  "direction": "up" | "down",
  "project_name": "...",
  "counterparty": "...",
  "contract_no": "...",
  "status": "pending" | "processing" | "completed" | "unpaid" | "rejected",
  "notes": "...",
  "amounts": [
    {"id": 101, "currency": "PHP", "amount": 100000.0},
    {"id": 102, "currency": "USD", "amount": 5000.0}
  ],
  "paid_summary": [
    {"currency": "PHP", "paid": 50000.0, "paid_payment_count": 1, "remaining": 50000.0, "fully_paid": false},
    {"currency": "USD", "paid": 0.0, "paid_payment_count": 0, "remaining": 5000.0, "fully_paid": false}
  ],
  "paid_at": "2026-08-13" | null,
  "attachments": [{"id": 1, "file_name": "...", "file_size": 12345}]
}
```

- `paid_summary[]` 顺序与 `amounts[]` 一致（按 sort_order / id）。
- `paid_at` 是最后一次付款的 `payment_date`（用于排序"最近有付款的"）；不等于"全部付清日期"。
- `fully_paid = true` 当且仅当 `paid >= amount - 0.01`。

### `GET /api/settlement-records/summary`

汇总统计卡（leader + admin）。`upstream` / `downstream` 各含：

```json
{
  "total":     {"PHP": {"amount": 100000, "count": 1}, ...},
  "completed": {...},
  "pending":   {...},
  "month":     {...},
  "paid":      {"PHP": {"amount": 50000, "count": 1}, ...},
  "per_currency": [
    {"currency": "PHP", "total": 100000, "count": 1, "paid": 50000, "unpaid": 50000, "paid_payment_count": 1},
    ...
  ]
}
```

`per_currency[]` 按字母序合并 total + paid 的币种集，供 UI 渲染"已付 X / 未付 Y / Z%"卡片。

## CRUD（admin only）

### `POST /api/settlement-records`

新建结算单。form-data：
- `direction` = `up` / `down`
- `project_name` / `counterparty` / `contract_no`
- `settle_date` = `YYYY-MM-DD`（必填）
- `status` = `pending` / `processing` / `completed` / `unpaid` / `rejected`
- `notes`
- `amounts[i][currency]` / `amounts[i][amount]`（i=0,1,2...，至少 1 行）

返回 `{ok, id}`。

### `PUT /api/settlement-records/<id>`

更新结算单。字段同上（amounts 会全量替换，attachments 是 append 模式）。

### `DELETE /api/settlement-records/<id>`

删除结算单（级联删除 amounts / attachments / payments）。

## 付款追踪（Payment Tracking）

每条结算单可挂多条 `settlement_payments`（按金额行记录），自动推导主表 `status`：
- 所有金额行都付清 → `completed`
- 有任一付款但未全付清 → `processing`
- 全无付款 → `unpaid`（不动 admin 手动设的 `pending` / `rejected`）

### `GET /api/settlement-records/<rid>/payments`

列出全部付款（含 voided）+ 按币种汇总（leader + admin）。

```json
{
  "ok": true,
  "payments": [
    {
      "id": 5,
      "settlement_amount_id": 101,
      "currency": "PHP",
      "amount": 50000.0,
      "payment_date": "2026-08-13",
      "payment_method": "bank",
      "reference_no": "PMT-001",
      "status": "confirmed" | "voided",
      "notes": "...",
      "created_by": "...",
      "created_at": "2026-08-13T10:00:00"
    }
  ],
  "per_currency": [
    {
      "settlement_amount_id": 101,
      "currency": "PHP",
      "total": 100000.0,
      "paid": 50000.0,
      "unpaid": 50000.0,
      "is_paid_in_full": false
    }
  ]
}
```

### `POST /api/settlement-records/<rid>/payments`

新增付款（admin only）。form-data：
- `amount_id` = `settlement_amounts.id`（必填）
- `amount` = 数字 > 0
- `payment_date` = `YYYY-MM-DD`
- `payment_method` = `bank` / `cash` / `cheque` / 其他字符串
- `reference_no` / `notes` 可选

**校验**：`Σ已付confirmed + 本次amount <= amount + 0.01`，否则 400 + `{error, already_paid, total}`。

返回 `{ok, id}`。

### `DELETE /api/settlement-payments/<pid>`

撤销付款（admin only）。软删除（status='voided'，audit 仍可见）。会自动触发 `_recompute_record_payment_state` 重算主表 status 和 paid_at。

## 附件

### `POST /download/settlement-attachment/<aid>`

下载附件（leader + admin，按 settlement_attachments.id）。

### 上传附件

通过 `POST /api/settlement-records/<rid>` (PUT) 的 `attachments[]` 字段上传，与 records 同事务。

## 错误码

| 码 | 含义 |
|---|---|
| 400 | 参数校验失败（如 overpay、amount_id 不是数字、必填字段缺失） |
| 401 | 未登录或会话过期 |
| 403 | 角色不足（如 director 调 admin only 端点） |
| 404 | 记录不存在 |
| 500 | 服务端异常（看 Vercel 日志 traceback） |
