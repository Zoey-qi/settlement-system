import time, urllib.request, urllib.parse, http.cookiejar, json
BASE = 'https://settlementsystem.vercel.app'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None

for i in range(6):
    time.sleep(15)
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    data = urllib.parse.urlencode({'username':'admin','password':'htglb888','department':''}).encode()
    req = urllib.request.Request(f'{BASE}/api/auth/login', data=data, method='POST')
    resp = op.open(req)
    j = json.loads(resp.read())

    op2 = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(cj))
    statuses = []
    for ep in ['/','/settlement','/submit','/templates','/config']:
        try:
            r = op2.open(f'{BASE}{ep}')
            statuses.append(f'{ep}:{r.status}')
        except urllib.error.HTTPError as e:
            statuses.append(f'{ep}:{e.code}')
    print(f't+{(i+1)*15}s:', ' '.join(statuses))