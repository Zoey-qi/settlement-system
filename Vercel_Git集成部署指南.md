# Vercel Git 集成部署（最稳方案）

## 为什么这是最稳的
- 你之前的部署是 CLI 模式（`vercel deploy`），每次都要手动跑，容易卡
- 改成 Git 集成后，**每次 `git push` 自动部署**，Vercel 自己拉代码
- 团队项目需要 owner 授权，但协作者可以通过 Git 集成触发部署

## 第一步：断开 Vercel 项目里旧的部署方式

1. 浏览器打开：**https://vercel.com/tiangjihu01-1213s-projects/settlement_system/settings**
2. 在左侧菜单找 **"Git"** 或 **"Connected Git Repository"** 部分
3. 如果显示 "Not connected" → 直接跳到第二步
4. 如果显示已连接的旧仓库（如果有）→ 点 **"Disconnect"**

## 第二步：连接 GitHub 仓库

1. 同一个 Settings 页面，找 **"Git Repository"** 卡片
2. 点击 **"Connect Git Repository"** 按钮
3. 在弹窗里选 **"GitHub"**
4. 如果提示授权 → 用 **你的 GitHub 账号（Zoey-qi）** 登录授权
5. 在仓库列表里找到 **`Zoey-qi/settlement-system`** → 点击 **"Import"**
6. 导入页面设置：
   - **Project Name**: `settlement_system`（保持）
   - **Framework Preset**: `Other`（不要选 Python）
   - **Root Directory**: `./`（空着）
   - **Build Command**: 空着
   - **Output Directory**: 空着
7. 点击 **"Deploy"**

## 第三步：等首次部署完成

- 第一次部署需要 3-5 分钟（编译 Python 环境）
- 部署完成后页面顶部会显示绿色 ✅ Production 域名
- 部署完成后页面自动跳到 Deployments 页面

## 第四步：验证部署成功

打开浏览器访问：**https://settlementsystem.vercel.app**

应该看到：
- ❌ 不再有 "部门文件" 链接
- ✅ 右上角有 "登录" 按钮
- 点击 "登录" 能进 `/login` 页面

## 以后更新代码

```bash
git add .
git commit -m "update"
git push origin main
```

Vercel 会**自动部署**，1-2 分钟生效。再也不用手动跑脚本了。

---

## 常见问题

### Q: 找不到 "Git Repository" 选项？
- 在 Settings 页面顶部找 **"Connected Git Repository"** 区域
- 如果页面有这个文字 → 点击旁边的 **"Connect"** 按钮

### Q: 提示 "You don't have permission"？
- 项目归 `tiangjihu01-1213s-projects` 团队所有
- 你需要联系团队 owner（`tiangjihu01` 那个账号）授权
- 或者请他帮你连接 Git 仓库

### Q: 部署成功但 404？
- 等 1-2 分钟（DNS 缓存）
- 检查 Vercel Deployments 页是否显示绿色 READY
- 看 Build Logs 有没有报错

---

**操作完后告诉我结果，我立刻跑 4 角色线上冒烟测试。**