"""回归测试：登录 → 上传 → 下载，确认上传链路完全恢复"""
import urllib.request, urllib.parse, json, http.cookiejar, ssl, uuid

ctx = ssl.create_default_context()
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cj),
    urllib.request.HTTPSHandler(context=ctx),
)

# 1. 登录拿 token
data = urllib.parse.urlencode({'username': 'admin', 'password': 'htglb888'}).encode()
req = urllib.request.Request(
    'https://settlementsystem.vercel.app/api/auth/login',
    data=data,
    headers={'Content-Type': 'application/x-www-form-urlencoded'},
)
r = opener.open(req, timeout=30)
login_resp = json.loads(r.read().decode())
token = login_resp.get('token')
auth_cookie = None
for c in cj:
    if c.name == 'auth_token':
        auth_cookie = c.value
print('1. login:', login_resp.get('role'), '/', login_resp.get('display_name'))
print('   auth_token cookie:', 'YES (len=%d)' % len(auth_cookie) if auth_cookie else 'NO')

# 2. 上传（用正确的 submission_type=file）
boundary = '----WebKitFormBoundary' + uuid.uuid4().hex


def field(name, value):
    return ('--' + boundary + '\r\n'
            'Content-Disposition: form-data; name="' + name + '"\r\n\r\n'
            + value + '\r\n').encode()


def file_field(name, filename, content):
    return ('--' + boundary + '\r\n'
            'Content-Disposition: form-data; name="' + name + '"; filename="' + filename + '"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + content + b'\r\n'


end = ('--' + boundary + '--\r\n').encode()

body = (
    field('month', '2026-08')
    + field('submission_type', 'file')
    + field('submitter', 'admin')
    + field('remarks', 'regress')
    + file_field('files', 'regress.xlsx', b'PK\x03\x04test data')
    + end
)

req2 = urllib.request.Request(
    'https://settlementsystem.vercel.app/submit/item/2?month=2026-08',
    data=body,
    headers={
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
        'X-Requested-With': 'XMLHttpRequest',
        'Cookie': 'auth_token=' + token,
    },
)
r2 = opener.open(req2, timeout=60)
upload_resp = json.loads(r2.read().decode())
print('2. upload: ok=%s, sub_id=%s, files=%d, file_name=%s'
      % (upload_resp.get('ok'),
         upload_resp.get('sub_id'),
         len(upload_resp.get('files', [])),
         upload_resp.get('file_name')))

# 3. 下载验证
files = upload_resp.get('files', [])
if not files:
    print('3. download: SKIP (no files returned)')
else:
    req3 = urllib.request.Request(
        'https://settlementsystem.vercel.app' + files[0]['download_url'],
        headers={'Cookie': 'auth_token=' + token},
    )
    r3 = opener.open(req3, timeout=30)
    dl_data = r3.read()
    expected = b'PK\x03\x04test data'
    print('3. download: size=%d, match=%s'
          % (len(dl_data), 'OK' if dl_data == expected else 'MISMATCH'))
