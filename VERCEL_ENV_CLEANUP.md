# Vercel 项目环境变量清理指南（Vercel Blob 相关）

> Vercel Storage Connect 自动注入 4 个 `*_BLOB_*` env 变量，其中 **2 个是项目代码根本没用**的冗余残留。本指南说明哪些安全删、哪些必留、删错的代价。

## 项目代码真正用到的 env

`settlement_system/app.py` 和 `db.py` 里所有 `os.environ.get()` 引用：

| Env 变量 | 必需？ | 用途 |
|---|---|---|
| `POSTGRES_URL` / `DATABASE_URL` / `POSTGRES_URL_NON_POOLING` | ✅ 必需 | Neon 数据库连接 |
| `BLOB_READ_WRITE_TOKEN` / `SETTLEMENT_BLOB_READ_WRITE_TOKEN` | ✅ 必需 | Vercel Blob 读写令牌 |
| `BLOB_STORE_ID` / `SETTLEMENT_BLOB_STORE_ID` | ⚪ 可选 | 已能从 token 解析（`_blob_store_id()` 第 113 行 fallback） |
| `BLOB_ACCESS` / `SETTLEMENT_BLOB_ACCESS` | ⚪ 可选 | 不存在时代码默认 `'private'` |
| `VERCEL` | ⚪ 可选 | Vercel 环境标志（db.py 用） |
| `VERCEL_BLOB_API_URL` | ⚪ 可选 | 默认 `https://vercel.com/api/blob`，自定义上传端点 |

## Vercel Storage Connect 自动注入的 env

| 自动注入的 env | 代码端用？ | 处理建议 |
|---|---|---|
| `BLOB_READ_WRITE_TOKEN` | ✅ 用 | **必留** |
| `BLOB_STORE_ID` | ⚪ 可选但已注入 | **可删**（代码从 token 解析） |
| `BLOB_ACCESS` | ⚪ 可选 | **没注入**（Vercel 不会自动注入 access） |
| `*_WEBHOOK_PUBLIC_KEY`（如 `BLOB_WEBHOOK_PUBLIC_KEY` 或 `SETTLEMENT_BLOB_WEBHOOK_PUBLIC_KEY`） | ❌ 完全不用 | **可删，纯残留** |

## 清理步骤

### 进入 Vercel 后台
1. https://vercel.com/dashboard → 选项目（`settlement-system`）
2. **Settings** → **Environment Variables**

### 检查当前变量

预期看到（截图会话里2026-08-15确认）：

```
SETTLEMENT_BLOB_READ_WRITE_TOKEN    vercel_blob_rw_xxxxx...   Production/Preview
SETTLEMENT_BLOB_STORE_ID            store_YTjbBAWDVoODnSIr    Production/Preview
SETTLEMENT_BLOB_WEBHOOK_PUBLIC_KEY  whsec_xxxxx...            Production/Preview   ← 删
```

如果有 `BLOB_STORE_ID`（**不带** `SETTLEMENT_` 前缀），那也是删 —— 这是初次连接时 prefix 含连字符失败导致 Vercel 回落到默认 prefix 的残留。

### 删除步骤（每条独立操作）

对每条要删的 env：
1. 找到变量行最右侧 `⋯` 菜单 → **Remove**
2. 弹窗确认 → **Remove**
3. 重复到全部删完

### 推荐删除顺序（从最安全到最激进）

1. **最先**：`SETTLEMENT_BLOB_WEBHOOK_PUBLIC_KEY`（代码完全不用）
2. **可选**：`SETTLEMENT_BLOB_STORE_ID`（代码可从 token 解析）
3. **不动**：`SETTLEMENT_BLOB_READ_WRITE_TOKEN`（一旦删了，Blob 完全挂掉，且 Rotate 才能恢复）

### 删除后验证

不需要 redeploy。Vercel 函数每次启动时重新读 env。但**冷启动才会刷新**，建议：

1. 等 5~10 分钟（让现有实例自然冷却）
2. 跑回归测试：
   ```bash
   cd settlement_system
   python scripts/regress_upload.py
   ```
   应看到 `1. login: OK / 2. upload: ok=True / 3. download: size=13, match=OK` 三段绿
3. 看 blob-status：
   ```bash
   curl https://settlementsystem.vercel.app/api/blob-status
   # 应返 enabled:true, store_id 与删除前一致
   ```

## 安全承诺

- `BLOB_STORE_ID` 是冗余加速缓存，删后下次冷启动从 token 重新解析（多 1 次正则），**不影响功能**
- `*_WEBHOOK_PUBLIC_KEY` 项目从设计到代码都没用，**0 副作用**

## 误删补救

| 误删的 env | 影响 | 补救 |
|---|---|---|
| `BLOB_READ_WRITE_TOKEN` | **灾难性**：所有上传/下载 500 | Settings → Storage → 选 store → **Rotate Credentials**（生成新 token 自动注入） |
| `BLOB_STORE_ID` | 无影响 | 不必补救 |
| `*_WEBHOOK_PUBLIC_KEY` | 无影响 | 不必补救 |

## 不删也行的理由

如果你担心未来可能要加 webhook 回调功能（Vercel Blob 支持 webhook 通知上传事件），`WEBHOOK_PUBLIC_KEY` 留着也无害（多几百字节环境变量而已）。但目前项目代码**完全没规划 webhook**，所以放心删。

## 时间线参考

- 2026-08-15 16:xx (UTC+8): Rotate Credentials 后 Vercel 自动重注 STORE_ID + WEBHOOK_PUBLIC_KEY
- 2026-08-15: 用户确认线上 `enabled:true, roundtrip:OK`
- 2026-08-15+: 按本指南清理冗余