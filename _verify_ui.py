import json
from app import app
app.config['TESTING'] = True

def login(user, pwd):
    c = app.test_client()
    r = c.post('/api/auth/login', data={'username': user, 'password': pwd})
    if r.status_code != 200:
        return None, None
    t = json.loads(r.get_data(as_text=True)).get('token')
    return c, t

with app.test_client() as c:
    r = c.get('/login')
    print('GET /login        ->', r.status_code, '| auth-aside:', 'auth-aside' in r.get_data(as_text=True))
    css = c.get('/static/css/style.css').get_data(as_text=True)
    print('GET /static css    -> #1E40AF:', '#1E40AF' in css, '| --c-gold:', '--c-gold:' in css, '| gold logo:', '#E8B84B' in css)

for name, u, p in [('admin','admin','htglb888'), ('leader','leader','pakil123456')]:
    c, t = login(u, p)
    if not c:
        print(f'{name} login FAILED'); continue
    c.set_cookie('auth_token', t)
    r = c.get('/')
    h = r.get_data(as_text=True)
    print(f'{name:6} GET /        ->', r.status_code, '| 仪表盘:', '仪表盘' in h, '| 侧栏品牌:', '帕基尔结算' in h, '| 金强调条class:', 'side-link' in h)
    rc = c.get('/config')
    print(f'{name:6} GET /config   ->', rc.status_code, '(admin 应200, leader 应302/403)')
