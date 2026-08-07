# GitHub Action 自动部署指南（最稳方案）

## 原理
不再用 Vercel CLI，改用 GitHub 自己的 CI 系统。代码 push 到 GitHub → GitHub Action 自动调用 Vercel API 部署。

## 为什么这个最稳
- ✅ 不用装 Node.js
- ✅ 不用 PATH 配置
- ✅ 不用 Vercel CLI
- ✅ 不用每次手动触发
- ✅ 团队项目支持
- ✅ 完全免费（GitHub 公开仓库 Actions 免费，私有仓库也有 2000 分钟/月）

## 第一步：获取 3 个 Secrets

### A. VERCEL_TOKEN
1. 浏览器打开：**https://vercel.com/account/tokens**
2. Name: `github-action`
3. Scope: **Full Account**
4. Expiration: **No Expiration**（或 1 Day）
5. 点击 **Create** → **立刻复制 token**（只显示一次！）
6. ⚠️ Token 形如 `vercel_pwd_xxxxxxxxx`

### B. VERCEL_ORG_ID
你需要在本地执行一次 `vercel link`（或 `vercel pull`）来生成 `.vercel/project.json`。

打开 PowerShell：
```powershell
cd "C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system"
& "C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules\.bin\vercel.cmd" link --yes --token "你的VERCEL_TOKEN"
```

执行后可能需要选择团队（选 `tiangjihu01-1213s-projects`），然后会生成 `.vercel/project.json`：

```powershell
Get-Content .vercel\project.json
```

输出会包含两个字段：
```json
{
  "orgId": "team_xxxxx",        ← 这是 VERCEL_ORG_ID
  "projectId": "prj_xxxxx"      ← 这是 VERCEL_PROJECT_ID
}
```

把这两个值复制下来。

### C. VERCEL_PROJECT_ID
就是上面那个 `projectId`。

## 第二步：把 3 个 Secrets 添加到 GitHub

1. 浏览器打开：**https://github.com/Zoey-qi/settlement-system/settings/secrets/actions/new**
2. 添加 3 个 Secret：

| Name | Value |
|------|-------|
| `VERCEL_TOKEN` | 你的 Vercel Token（步骤 A） |
| `VERCEL_ORG_ID` | 步骤 B 拿到的 `team_xxxxx` |
| `VERCEL_PROJECT_ID` | 步骤 B 拿到的 `prj_xxxxx` |

3. 每添加一个点 **"Add secret"**

## 第三步：创建 GitHub Action 工作流

在你的项目里创建文件 `.github/workflows/deploy.yml`：

```yaml
name: Deploy to Vercel

on:
  push:
    branches:
      - main
  workflow_dispatch:  # 允许手动触发

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install Vercel CLI
        run: npm install --global vercel@latest

      - uses: amondnet/vercel-action@v42
        with:
          vercel-token: ${{ secrets.VERCEL_TOKEN }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          vercel-org-id: ${{ secrets.VERCEL_ORG_ID }}
          vercel-project-id: ${{ secrets.VERCEL_PROJECT_ID }}
          vercel-args: '--prod --yes'
```

## 第四步：推送触发部署

在本地 PowerShell：

```powershell
cd "C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system"
git add .github/workflows/deploy.yml
git commit -m "ci: GitHub Action 自动部署"
git push origin main
```

## 第五步：查看部署进度

1. 打开：**https://github.com/Zoey-qi/settlement-system/actions**
2. 看到 `Deploy to Vercel` workflow 正在跑（黄色转圈）
3. 等 2-3 分钟变绿勾 ✅

部署完成！访问：**https://settlementsystem.vercel.app**

---

## ⚠️ 注意事项

### 团队项目权限
你（Zoey-qi）创建了仓库，但 Vercel 项目归 `tiangjihu01-1213s-projects` 团队所有。

GitHub Action 用的是你的 Vercel Token（你账号的权限），不是 GitHub OAuth。所以**只要你的 Vercel Token 授权覆盖了团队项目，就能部署**。

如果 Token 是 Full Account scope，应该有权限。

### 如果失败
打开 GitHub Actions 页面，看失败步骤的报错，把错误截图给我。

---

**完成上面 4 步后告诉我结果，我立即跑 4 角色线上冒烟测试。**