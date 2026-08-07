from app import app

app.config['TESTING'] = True
with app.test_client() as c:
    # 1) vendor 静态文件内容
    for path in ['/static/vendor/bootstrap/css/bootstrap.min.css',
                 '/static/vendor/bootstrap/js/bootstrap.bundle.min.js']:
        r = c.get(path)
        data = r.get_data()
        print(f'{path}\n  status={r.status_code}  bytes={len(data)}  head={data[:40]!r}')

    # 2) /login 引用检查
    r = c.get('/login')
    html = r.get_data(as_text=True)
    print('\n/login 引用:')
    print('  本地 bootstrap css:', 'vendor/bootstrap/css/bootstrap.min.css' in html)
    print('  本地 bootstrap js :', 'vendor/bootstrap/js/bootstrap.bundle.min.js' in html)
    print('  残留 CDN bootstrap :', 'cdn.jsdelivr.net/npm/bootstrap' in html)

    # 3) 四角色导航 — 结算金额可见性
    import json
    NAV = ['仪表盘','结算金额','提交数据','汇总视图','模板文件','历史记录','使用指引','配置管理','系统状态']
    cases = [('admin','htglb888'), ('leader','pakil123456')]
    # director / liaison 密码从 DB 拿
    import sqlite3
    conn = sqlite3.connect('settlement.db'); conn.row_factory = sqlite3.Row
    # 本地 sqlite 空，改用用户表里已知账号（若为空则跳过）
    rows = conn.execute("SELECT username, password_plain, role FROM users WHERE username LIKE 'director_%' OR username LIKE 'liaison_%' LIMIT 2").fetchall()
    conn.close()
    for row in rows:
        cases.append((row['username'], row['password_plain']))

    for user, pwd in cases:
        r = c.post('/api/auth/login', data={'username': user, 'password': pwd})
        if r.status_code != 200:
            print(f'\n[{user}] 登录失败 {r.status_code}'); continue
        token = json.loads(r.get_data(as_text=True)).get('token')
        c.set_cookie('auth_token', token)
        r = c.get('/')
        h = r.get_data(as_text=True)
        vis = {n: (n in h) for n in NAV}
        print(f'\n[{user}] 结算金额可见={vis["结算金额"]} | 全可见={[n for n,v in vis.items() if v]}')
        c.post('/api/auth/logout')
