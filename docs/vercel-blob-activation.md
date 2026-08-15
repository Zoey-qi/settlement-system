# Vercel Blob 激活指引（帕基尔结算系统）

## 为什么要激活

当前 `save_upload_file` 是按以下优先级选择存储：

| 优先级 | 存储 | 适用场景 | 当前状态 |
|---|---|---|---|
| 1 | **Vercel Blob**（CDN 全球分发） | >100KB 文件、高频下载 | ❌ **未激活**（环境变量缺失） |
| 2 | Neon Postgres `BYTEA` 列 | 小文件、需事务一致性 | ✅ **生效中**（你的文件现在都存这里） |
| 3 | 本地磁盘 | 本地开发 | ✅ 仅本地 |

激活 Blob 的好处：

1. **数据库体积不会撑爆**：当前所有上传文件都进 Neon `BYTEA` 列，每上传 1MB 数据库就大 1MB。Neon 免费版 0.5GB 限额，按月均 100 个 5MB 文件算 = 半年到顶。激活 Blob 后文件不进 DB，只存一个 URL 引用。
2. **下载走 Vercel CDN**：Neon 的 BLOB 是流式读 Vercel Function（每下载都触发冷启动），Blob 走 CDN 边缘缓存（用户直连 `*.public.blob.vercel-storage.com`，零服务器开销）。
3. **突破 4.5MB 上传限制**：Vercel Function 请求体最大 4.5MB；走 Blob client upload 可传 GB 级文件（前端直传 Blob，文件不经过你的 Function）。
4. **真·全球加速**：Blob 默认在 20 个区域有 hub，菲律宾用户下载会就近命中。

## 激活步骤（一次性，5 分钟）

### 1. 在 Vercel 后台创建 Blob store

1. 打开 https://vercel.com/dashboard
2. 进入项目 `settlement-system`
3. 顶部 tab 选 **Storage**（在 Overview / Deployments / Analytics 等旁边）
4. 点 **Create Database**
5. 数据库类型选 **Blob**
6. 点 **Continue**
7. 设置：
   - **Name**：`settlement-files`（或随便起）
   - **Access**：选 **Public**（如果文件可以公开下载）**或 Private**（如果只有登录用户能下载）
     - 项目当前所有上传都是登录后下载，所以选 **Private** 更安全。但 Private 仍要走 Vercel Function 流式读，没有 CDN 加速；如果想兼顾安全和速度，选 **Public**（文件 URL 任何人能访问，对小工程无所谓）
   - **Region**：选 **Singapore (sin1)**（离菲律宾最近，Vercel 项目本身也部署在 sin1）
8. 点 **Create a new Blob store**
9. 创建成功后，**自动**给项目注入环境变量：
   - `BLOB_READ_WRITE_TOKEN`（永久 token，代码读它）
   - `BLOB_STORE_ID` + `VERCEL_OIDC_TOKEN`（OIDC 短期 token，自动轮转，更安全）

### 2. 验证生效

激活后访问（admin 角色）：`https://settlementsystem.vercel.app/api/blob-status`

应该返回：
```json
{
  "enabled": true,
  "store_id": "store_xxxxxxxxxxxx",
  "roundtrip": "OK",
  "latency_ms": 230
}
```

返回 `enabled: false` 表示环境变量没注入（重新部署一次：Settings → Deployments → 最新一次 → Redeploy）。

### 3. 激活后的真实效果

- 上传文件 → 走 Blob（URL 形如 `https://store_xxx.public.blob.vercel-storage.com/item_submissions/20260815_xxx.xlsx`）
- 数据库 `item_submissions` 表新增 `blob_url` / `blob_pathname` 列存引用
- 下载文件 → 浏览器直连 Blob CDN（< 100ms 全球）
- DB 体积不再增长

### 4. 回滚机制（自动）

代码里 `save_upload_file` 有 try/except 兜底：Blob 调用失败 → 自动回退到 Neon `BYTEA` 存储。**激活后也不会破坏现有功能**。

## 不激活会怎样

继续走 Neon BYTEA 存储，对当前用户量（< 50 人/月）完全够用。但：
- 每月上传文件 > 100MB 后建议激活（监控 Neon 存储用量：https://console.neon.tech → 项目 → Storage）
- 大文件（> 4.5MB）目前会被 Vercel Function 截断报错（虽然你目前都没遇到）

## 我能帮你做的

激活后告诉我，我会：
1. 跑 `/api/blob-status` 验证生效
2. 上传一个测试文件确认 Blob URL 已生成
3. 在 `/summary` 页加一个"存储用量"卡片显示 Blob vs DB 占用