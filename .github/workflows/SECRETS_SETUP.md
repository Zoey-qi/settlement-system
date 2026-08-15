# GitHub Actions Secrets 配置指南（regress-upload workflow）

> 让 `.github/workflows/regress-upload.yml` 在 push 时能自动跑端到端回归测试所需的最小配置。

## 用途

`.github/workflows/regress-upload.yml` 每次 `main` 分支收到 push 时（或手动 `workflow_dispatch`）会跑 `scripts/regress_upload.py`，调用线上环境做完整链路验证：

```
登录 → 上传 .xlsx → 下载 → 字节对比
```

脚本默认账号是 `admin / htglb888`，但**线上 admin 密码是部署时的值，硬编码不安全**。改成从 Secrets 读。

## 需要配置的 Secrets

进入 repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，加两条：

| Name | Value 来源 | 说明 |
|---|---|---|
| `TEST_USERNAME` | 任意内置账号（如 `admin`） | 登录用户名。**建议别用 admin**（admin 写权限高，CI 误触发破坏数据） |
| `TEST_PASSWORD` | 对应账号的明文密码 | 部署时初始化时由项目领导班子记录。**只放明文到 repo secrets**，绝不进代码 |

### 推荐账号选择

CI 推荐用一个**只读权限**的 `liaison_*`（部门联络人），而不是 admin。理由：

- admin 有 `/config` `/templates` 等写权限，CI 误操作可能改系统配置
- `liaison_*` 只能读仪表盘 + 上传提交 + 读 templates；上传/下载路径已被验证

**前提**：项目目前**没有 `liaison_test` 这类专用 CI 账号**。两条路：

1. （推荐）**手工到线上** `/config` 页加一个 `liaison_test` 联络人账号
2. （备选）**短期**先用 `admin`，风险可接受（CI 只上传 13 字节测试文件，不改配置）

## 配置后验证

1. 进入 repo → Actions → "Regression - Upload Pipeline"
2. 点 "Run workflow" → 选 main → Run
3. 等 1~2 分钟，看到绿色 ✅ = 配置成功

失败的常见原因：

| 报错 | 原因 | 修复 |
|---|---|---|
| `1. login: FAILED (401)` | 用户名或密码错 | 到线上 `/login` 验证账号有效 |
| `2. upload: FAILED (403)` | 该账号没提交权限 | 换 `admin` 或加 `liaison_*` 账号 |
| `1. login: FAILED (timeout)` | Vercel 冷启动 + IP 限流 | 重跑一次 |
| `secret TEST_USERNAME not found` | Secrets 没配或名字打错 | 检查 Settings → Secrets |

## 安全性提醒

- Secrets 在 repo settings 里**只对 Actions 可见**，对 PR 来自 fork 的 workflow **不可见**（避免泄密）
- 不要把 Secrets 写到 workflow 输出里（`${{ secrets.* }}` 会自动脱敏为 `***`）
- 若怀疑泄露：到 Settings → Secrets → "Update" 重置

## 关联文档

- 回归脚本：`scripts/regress_upload.py`（支持 BASE_URL / TEST_USERNAME / TEST_PASSWORD env）
- 触发条件：`regress-upload.yml` 的 `paths:` 过滤器，只在 `app.py` / `db.py` / 模板 / workflow / 脚本改动时跑（避免每次前端微调都触发）