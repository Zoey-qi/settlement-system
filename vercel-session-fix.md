# Vercel Session 持久化修复

## 问题现象

部署到 Vercel 后，**登录后访问第一个页面 OK，但切换模块（点击侧边栏其他导航项）就立刻跳回登录页**。

## 根因

旧的 `user_sessions` 字典是**进程内内存**：

```python
user_sessions = {}  # {token: {'user': dict, 'expire': ts}}
```

Vercel 用 Serverless 函数：
- 每次请求可能落在**不同的 Lambda 容器**
- 容器被复用时内存还在；冷启动时内存**全部清空**
- 用户带着 cookie 跳到下一个模块时，新容器里的 `user_sessions` 字典是空的 → token 找不到 → `parse_token` 返回 `None` → `enforce_login` 重定向到 `/login`

本地开发看不出来（Flask 单进程、内存常驻），所以 smoke test 一直全绿，直到上了 Vercel 才暴露。

## 修复方案

把 session 从内存字典迁到 **PostgreSQL / SQLite 数据库**：

### 1. 新增表 `user_sessions`

```sql
CREATE TABLE user_sessions (
    token        TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL,
    department   TEXT,
    phone        TEXT,
    expire_ts    BIGINT NOT NULL,  -- Unix 时间戳，秒
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

由 `db.py` 的 `init_schema()` 统一创建，SQLite / PostgreSQL 双兼容。

### 2. 三个新辅助函数

- `save_session(token, user_dict)`：登录成功后写入 DB
- `parse_token(token)`：从 DB 读取；命中后做**滑动续期**（UPDATE expire_ts）
- `delete_session(token)`：登出删除

### 3. 进程内缓存（性能优化）

每个请求都查 DB 太重，加一层内存缓存：

```python
_session_cache = {}  # {token: (user_dict, expire_ts)}
_SESSION_CACHE_TTL = 30  # 秒
```

- 同一容器内的请求走缓存，30 秒内不重复打 DB
- 容器冷启动时缓存自然失效，token 自动回 DB 查询
- 登出时同步清缓存

### 4. SQL 适配

- SQLite：`INSERT OR REPLACE` / `?`
- PostgreSQL：`ON CONFLICT (token) DO UPDATE SET expire_ts = EXCLUDED.expire_ts` / `%s`

## 验证

### 本地

```
schema init ok, USE_POSTGRES = False
login -> ok=True, role=admin
OK /                  -> 200
OK /settlement        -> 200
OK /submit            -> 200
OK /summary           -> 200
OK /templates         -> 200
OK /config            -> 200
OK /history           -> 200
OK /guide             -> 200
OK /system-status     -> 200
```

### 模拟 Vercel 冷启动

```
container1 / -> 200
clear _session_cache (simulate cold start): cache_size was 1
cache_size now 0
container2 / (cold start, same token) -> 200 ✅ 修复前是 302
container2 /settlement        -> 200
container2 /submit            -> 200
container2 /templates         -> 200
```

## 注意事项

- Vercel 第一次冷启动时 `user_sessions` 表需要先被 `init_schema` 创建——这本来就在 `app.py` 启动流程里，每次冷启动都会跑一次，所以表一定会被创建
- 用户**不会**需要重新登录（除非 8 小时 TTL 到期）
- TTL 仍是 8 小时，与之前一致
- `parse_token` 命中 DB 后做"滑动续期"，用户活跃时会自动延长