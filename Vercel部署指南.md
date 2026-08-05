# 帕基尔结算系统 - Vercel 部署指南（免费）

## 你将得到什么
- 一个 `https://xxx.vercel.app` 的公网链接
- HTTPS 加密（没有"不安全"警告）
- 任何网络的人都能访问
- 7×24 在线，不依赖你的电脑
- 全部免费

---

## 第 1 步：注册 GitHub 账号（2分钟）

1. 打开 https://github.com/signup
2. 用邮箱注册（免费的）
3. 验证邮箱

## 第 2 步：创建 GitHub 仓库并上传代码（3分钟）

1. 登录 GitHub，点右上角 **+** → **New repository**
2. 仓库名填 `pakil-settlement`，选 **Private**（私有），点 **Create repository**
3. 在本电脑打开文件夹 `C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system`
4. 在空白处右键 → **Git Bash Here**（或打开终端 cd 到该目录）
5. 依次执行以下命令（替换你的邮箱和用户名）：

```bash
git config user.name "你的名字"
git config user.email "你的邮箱@example.com"
git remote add origin https://github.com/你的用户名/pakil-settlement.git
git push -u origin main
```

6. 输入 GitHub 用户名和密码（或 Personal Access Token）
7. 等待上传完成，刷新 GitHub 页面能看到代码就成功了

## 第 3 步：注册 Vercel 账号（1分钟）

1. 打开 https://vercel.com/signup
2. 点 **Continue with GitHub**，用 GitHub 账号直接登录
3. 授权 Vercel 访问你的 GitHub

## 第 4 步：创建 Vercel Postgres 数据库（2分钟）

1. 登录 Vercel 后，进入 https://vercel.com/dashboard
2. 点顶部 **Storage** 标签
3. 点 **Create Database** → 选 **Postgres** (Neon)
4. 名称填 `pakil-db`，点 **Create**
5. 创建完成后，找到 **Connect to your code** 部分
6. 复制 `POSTGRES_URL` 的值（一长串以 `postgres://` 开头的字符串）

## 第 5 步：部署项目（2分钟）

1. 回到 Vercel Dashboard，点 **Add New...** → **Project**
2. 在列表中找到 `pakil-settlement` 仓库，点 **Import**
3. 展开 **Environment Variables** 部分
4. 添加一个变量：
   - Name: `POSTGRES_URL`
   - Value: 粘贴第4步复制的连接字符串
5. 点 **Deploy**
6. 等待 1-2 分钟构建完成

## 第 6 步：获取链接

部署完成后，Vercel 会给你一个链接，类似：
```
https://pakil-settlement-xxx.vercel.app
```

**这就是你的永久链接！** 发给任何人，任何网络都能打开。

---

## 部署后要做的事

1. **重新上传模板文件**：进入「模板文件」页面，上传20个模板文件
2. **编辑使用指引**：进入「使用指引」页面，确认费率和内容
3. **检查部门配置**：进入「配置管理」页面，确认12项配置和30个条目

> 系统首次访问会自动创建数据库表和默认数据（部门、任务配置、费率等），不需要手动操作。

---

## 常见问题

**Q: 部署后打开报错怎么办？**
A: 在 Vercel Dashboard 点项目名 → **Logs** 查看错误日志

**Q: 免费额度够用吗？**
A: Vercel 免费版：每月 100GB 流量、100 小时函数执行。对一个项目部月度结算系统绰绰有余。Postgres 免费：256MB 存储，足够存几万条记录。

**Q: 数据安全吗？**
A: Vercel Postgres 由 Neon 提供基础设施，自动备份，数据中心在 AWS。比存在个人电脑上更安全。

**Q: 以后改了代码怎么更新？**
A: 本地修改后 `git push`，Vercel 会自动重新部署
