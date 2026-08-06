# UI 重构：图标迁移 + 登录页升级

## 1. 问题与意图

**用户反馈**（2026-08-06）：
- 顶栏图标是白的（白方块）—— 排查定位为 bootstrap-icons 字体在用户网络下加载失败
- 登录页有点单调 —— 需要更有层次感
- 程序代码精炼 —— 简化冗余

## 2. 整体方案

### 2.1 消除图标字体依赖：SVG Sprite 系统（T-1）

**问题**：bootstrap-icons 1.11.3 的 woff2 文件在某些网络环境（包括 Vercel CDN 抖动 / 公司代理）下加载失败，导致 `<i class="bi bi-xxx">` 渲染为空，旁边的按钮背景就显示成白方块。

**方案**：自建 SVG 精灵 + Jinja 宏。

| 文件 | 作用 |
|---|---|
| `templates/_icons.html` | 96 个 `<symbol>` 定义（覆盖 87 个用到的图标 + 9 个备用），全部 24×24 viewBox |
| `templates/_macros.html` | `{{ ui.icon('list', '', 16) }}` 宏，渲染 `<svg><use href="#i-list"/></svg>` |
| `base.html` | `{% include '_icons.html' %}` 注入精灵本体 |

**优点**：
- 0 个 HTTP 请求（精灵一次性渲染，所有图标复用）
- 跨网络/代理稳定：纯 DOM 节点，不依赖 woff2
- 颜色/尺寸用 CSS 控制（`.icon` / `width: 24px`）

### 2.2 模板精炼：数据驱动 sidebar（T-2）

`base.html` 原本 3 组侧栏导航（main / admin / staff）每条都是单独 `<a class="side-link">`。
重构后用 3 个 Python 列表 + `for` 循环渲染，**删除 25 行重复 HTML**。

```jinja
{% set nav_main = [
    ('dashboard', 'speedometer2', '仪表盘'),
    ('settlement_page', 'cash-stack', '结算金额'),
] %}
{% for ep, ic, lbl in nav_main %}
<a class="side-link {% if request.endpoint == ep %}active{% endif %}"
   href="{{ url_for(ep) }}">{{ ui.icon(ic, '', 18) }}<span>{{ lbl }}</span></a>
{% endfor %}
```

### 2.3 登录页升级：分层、信息密度、视觉层次（T-3）

**新增视觉元素**：
- 左侧背景：双 radial-gradient + 1px 网格 + 浮动光斑（`@keyframes pulse`）
- 左侧品牌区：项目徽标 + 中英副标题 + 4 条功能亮点 + 3 项数据 stat
- 右侧登录卡片：
  - 顶/底辉光（`::before` / `::after` 伪元素 + `blur(40px)`）
  - 顶部角色徽章（`shield-lock-fill` 图标 + 0.6px 描边圆角）
  - 输入框聚焦时左侧图标变主色 + `box-shadow` 渐显
  - 底部 `.auth-quick` 三栏业务提醒：截止日期 / 当前参与 / 本月新增
  - 密码框右侧显隐切换按钮（鼠标移入变色）
- 移动端响应式：`<768px` 时左侧品牌区隐藏，登录卡片铺满

**配套文件**：`static/css/login.css`（仅 login 页面加载，base.html 条件注入）

### 2.4 批量迁移脚本

`migrate_icons.py` — 一次性把 14 个模板里的 246 处 `<i class="bi bi-xxx">` 全部替换为 `{{ ui.icon('xxx', 'bi', size) }}`。

> 注：迁移脚本保留 `'bi'` 作为 hover 时的过渡类，但为了清洁也可以去掉；当前保留是因为某些 CSS 选择器 `.icon.bi:hover` 可能依赖。

## 3. 验证结果

| 角色 | 页面 | HTTP | SVG | `<use>` | 残留 `bi-` |
|---|---|---|---|---|---|
| 公开 | `/login` | 200 | 20 | 19 | 0 |
| admin | `/` | 200 | 87 | 86 | 0 |
| admin | `/settlement` | 200 | 25 | 24 | 0 |
| admin | `/submit` | 200 | 30 | 29 | 0 |
| admin | `/summary` | 200 | 76 | 75 | 0 |
| admin | `/history` | 200 | 16 | 15 | 0 |
| admin | `/guide` | 200 | 57 | 56 | 0 |
| admin | `/config` | 200 | 61 | 60 | 0 |
| admin | `/templates` | 200 | 161 | 160 | 0 |
| admin | `/system-status` | 200 | 31 | 30 | 0 |
| leader | `/` | 200 | 79 | 78 | 0 |
| leader | `/settlement` | 200 | 22 | 21 | 0 |
| director | `/` | 200 | 36 | 35 | 0 |
| director | `/submit` | 200 | 17 | 16 | 0 |
| director | `/summary` | 200 | 30 | 29 | 0 |
| director | `/history` | 200 | 13 | 12 | 0 |
| director | `/guide` | 200 | 54 | 53 | 0 |

**全部 17 个路由 200，0 个 `bi-` 残留。**

## 4. 文件清单

```
新增：
  templates/_icons.html          12 KB   SVG 精灵（96 symbols）
  templates/_macros.html        0.5 KB   icon 宏
  static/css/login.css          6 KB    login 专用样式
  migrate_icons.py              2 KB    一次性迁移脚本
  ui-redesign-overview.md       ← 本文件

修改：
  templates/base.html           重构：数据驱动 sidebar，移除 bootstrap-icons 字体 link
  templates/login.html          重写：双栏布局 + 业务提醒 + 视觉层次
  templates/dashboard.html      ~30 处 bi- → ui.icon
  templates/config.html         ~13 处 bi- → ui.icon
  templates/error.html          2 处 bi- → ui.icon
  templates/guide.html          ~35 处 bi- → ui.icon
  templates/history.html        6 处 bi- → ui.icon
  templates/settlement.html     ~12 处 bi- → ui.icon
  templates/submit.html         ~15 处 bi- → ui.icon
  templates/submit_list.html    4 处 bi- → ui.icon
  templates/submit_task.html    ~15 处 bi- → ui.icon
  templates/summary.html        ~12 处 bi- → ui.icon
  templates/system_status.html  ~10 处 bi- → ui.icon
  templates/templates.html      ~15 处 bi- → ui.icon
  templates/department_files.html  5 处 bi- → ui.icon（页面已下线，保留无害）
```

## 5. 部署

`git push origin main` → Vercel 自动部署。零新建环境变量，零数据库迁移。

## 6. 后续可选

- 删除 `static/icons/`（383KB bootstrap-icons 字体文件，已不再被引用）—— 当前被 safe-delete 拦截，需要 PowerShell 手动删除
- `templates/_icons.html` 可按需裁剪（现在 96 个 symbol，模板只用 87 个）
- `login.css` 配合 `<768px` 移动端体验，可继续打磨
