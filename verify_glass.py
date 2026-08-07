import app as A

c = A.app.test_client()
r = c.post('/api/auth/login', data={'username': 'admin', 'password': 'htglb888', 'department': ''})
tok = r.get_json().get('token')

c2 = A.app.test_client()
c2.set_cookie('auth_token', tok)

pages = [
    ('/', 'dashboard'), ('/submit', 'submit'), ('/summary', 'summary'),
    ('/settlement', 'settlement'), ('/templates', 'templates'),
    ('/config', 'config'), ('/history', 'history'),
    ('/guide', 'guide'), ('/system-status', 'system_status'),
]
print('=== authed pages (expect 200 + glass markers) ===')
for path, name in pages:
    h = c2.get(path).data.decode('utf-8', 'ignore')
    ok = 'bg-orbs' in h
    print(f'{name:14} http={c2.get(path).status_code}  bg-orbs={ok}  champion_left={("is-champion" in h)}  rank_champ_tag={("rank-tag champ" in h)}')

print('=== login page (expect 200 + auth-orb + glass) ===')
lr = c.get('/login').data.decode('utf-8', 'ignore')
print('login http=', c.get('/login').status_code, ' auth-orb=', 'auth-orb' in lr, ' bg-orbs=', 'bg-orbs' in lr)

print('=== css markers ===')
css = c.get('/static/css/style.css').data.decode('utf-8', 'ignore')
for m in ['--glass-blur', '.bg-orbs', '.orb-1', '.auth-orb', 'backdrop-filter', '.modal-content', 'is-champion', 'rank-tag.champ']:
    print(f'  {m:20} present={m in css}')

print('=== auth gate (expect 302) ===')
print('root no-auth http=', c.get('/').status_code)
