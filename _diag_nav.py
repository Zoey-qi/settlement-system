import json
from app import app

app.config['TESTING'] = True
NAV = ['仪表盘','结算金额','提交数据','汇总视图','模板文件','历史记录','使用指引','配置管理','系统状态']

with app.test_client() as c:
    for user, pwd in [('director_scdd','scddb111'), ('liaison_scdd','scddbabc')]:
        r = c.post('/api/auth/login', data={'username': user, 'password': pwd})
        assert r.status_code == 200, f'{user} login {r.status_code}'
        token = json.loads(r.get_data(as_text=True)).get('token')
        c.set_cookie('auth_token', token)
        r = c.get('/')
        h = r.get_data(as_text=True)
        vis = {n: (n in h) for n in NAV}
        print(f'[{user}] 结算金额可见={vis["结算金额"]} | 可见导航={[n for n,v in vis.items() if v]}')
        r2 = c.get('/settlement')
        print(f'   直访 /settlement -> {r2.status_code} (期望 302/403)')
        c.post('/api/auth/logout')
