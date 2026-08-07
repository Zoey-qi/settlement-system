import app as A

client = A.app.test_client()

# 登录 admin 拿 token
r = client.post('/api/auth/login', data={'username': 'admin', 'password': 'htglb888', 'department': ''})
print('[login]', r.status_code, r.get_json().get('role'))
token = r.get_json().get('token')

# 用 cookie 维持会话
c = A.app.test_client()
c.set_cookie('auth_token', token)

pages = [
    ('/', 'dashboard'),
    ('/settlement', 'settlement'),
    ('/config', 'config'),
    ('/templates', 'templates'),
    ('/guide', 'guide'),
    ('/system-status', 'system_status'),
    ('/history', 'history'),
    ('/summary', 'summary'),
    ('/login', 'login'),
]

for path, name in pages:
    try:
        resp = c.get(path)
        html = resp.data.decode('utf-8', 'ignore')
        ph = 'page-header' in html or 'ph-title' in html
        print(f'[{name:12}] status={resp.status_code}  page-header={ph}')
    except Exception as e:
        print(f'[{name:12}] ERROR {e}')

# 未登录拦截检查
gu = A.app.test_client()
print('[guest /]     status=', gu.get('/').status_code, '(expect 302)')
