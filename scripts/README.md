# 回归测试脚本

## 用途
- 诊断生产环境关键路径故障
- 部署后冒烟验证（避免推送后才发现 500）
- CI/CD 集成（如未来加 GitHub Actions）

## 文件说明
- `regress_upload.py`：**上传链路端到端测试**。登录 → multipart 上传 → 下载比对内容。复现 commit `f18d0f2` 修复的真实场景。
- `smoke_test.py`：**4 角色 × 关键路径冒烟**。admin/leader/director/liaison 各登录后访问各自允许的页面，确认权限矩阵完整。
- `test.xlsx`：**最小测试样本**（119 字节，合法 zip 头）。

## 用法
```bash
# Python 3.13+，urllib 标准库无需额外依赖
python scripts/regress_upload.py
python scripts/smoke_test.py
```

## 关键踩坑（来自这些脚本的开发过程）
1. **cookie 名**：`auth_token`（不是 `_auth_token`，前端 `templates/base.html` JS 设置的）
2. **`submission_type` 枚举**：`'file'` 或 `'none'`，**不是 `'upload'`**
3. **Git Bash curl 读 Windows 路径会失败**，必须用 Python urllib 手工拼 multipart
4. **前端 fetch 默认带同源 cookie**，所以测试时也要手动 `Cookie: auth_token=...` header

## 历史
- 2026-08-15：commit `f18d0f2` 修复 `submit_item` 4→6 元组解包 bug 后创建，作为长期回归测试。
