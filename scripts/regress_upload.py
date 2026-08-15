"""回归测试：登录 → 上传 → 下载，确认上传链路完全恢复。

支持环境变量覆盖（让 GitHub Actions 能跑任意环境 + 任意账号）：
  BASE_URL       默认 https://settlementsystem.vercel.app
  TEST_USERNAME  默认 admin
  TEST_PASSWORD  默认 htglb888
  TEST_ITEM_ID   默认 2
  TEST_MONTH     默认 2026-08

本地直接 python scripts/regress_upload.py 即可；CI 只需配 secrets 即可切换环境。
"""
import os
import sys
import urllib.request, urllib.parse, json, http.cookiejar, ssl, uuid

BASE_URL = os.environ.get('BASE_URL', 'https://settlementsystem.vercel.app').rstrip('/')
USERNAME = os.environ.get('TEST_USERNAME', 'admin')
PASSWORD = os.environ.get('TEST_PASSWORD', 'htglb888')
ITEM_ID = int(os.environ.get('TEST_ITEM_ID', '2'))
MONTH = os.environ.get('TEST_MONTH', '2026-08')
PAYLOAD = b'PK\x03\x04test data'  # 13 字节，模拟极简 xlsx
# 隔离模式：开启后上传完成后自动 unsubmit，不在生产数据里残留测试条目。
# CI 默认开启；本地手动跑也可设 ISOLATED=0 保留提交用于人工查看。
ISOLATED = os.environ.get('TEST_ISOLATED', '1') != '0'

print(f'=== regress_upload against {BASE_URL} (user={USERNAME}, item={ITEM_ID}, month={MONTH}) ===')

ctx = ssl.create_default_context()
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cj),
    urllib.request.HTTPSHandler(context=ctx),
)

# 1. 登录拿 token
try:
    data = urllib.parse.urlencode({'username': USERNAME, 'password': PASSWORD}).encode()
    req = urllib.request.Request(
        f'{BASE_URL}/api/auth/login',
        data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    r = opener.open(req, timeout=30)
    login_resp = json.loads(r.read().decode())
except Exception as e:
    print(f'1. login: FAILED ({type(e).__name__}: {e})')
    sys.exit(1)

token = login_resp.get('token')
if not token:
    print(f'1. login: FAILED (no token in response: {login_resp})')
    sys.exit(1)
print(f'1. login: OK (role={login_resp.get("role")})')

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
    field('month', MONTH)
    + field('submission_type', 'file')
    + field('submitter', USERNAME)
    + field('remarks', 'regress')
    + file_field('files', 'regress.xlsx', PAYLOAD)
    + end
)

try:
    req2 = urllib.request.Request(
        f'{BASE_URL}/submit/item/{ITEM_ID}?month={MONTH}',
        data=body,
        headers={
            'Content-Type': 'multipart/form-data; boundary=' + boundary,
            'X-Requested-With': 'XMLHttpRequest',
            'Cookie': 'auth_token=' + token,
        },
    )
    r2 = opener.open(req2, timeout=60)
    upload_resp = json.loads(r2.read().decode())
except Exception as e:
    print(f'2. upload: FAILED ({type(e).__name__}: {e})')
    sys.exit(1)

if not upload_resp.get('ok'):
    print(f'2. upload: FAILED (response: {upload_resp})')
    sys.exit(1)
print('2. upload: ok=%s, sub_id=%s, files=%d, file_name=%s'
      % (upload_resp.get('ok'),
         upload_resp.get('sub_id'),
         len(upload_resp.get('files', [])),
         upload_resp.get('file_name')))

# 3. 下载验证
files = upload_resp.get('files', [])
if not files:
    print('3. download: SKIP (no files returned)')
    sys.exit(1)
try:
    req3 = urllib.request.Request(
        BASE_URL + files[0]['download_url'],
        headers={'Cookie': 'auth_token=' + token},
    )
    r3 = opener.open(req3, timeout=30)
    dl_data = r3.read()
except Exception as e:
    print(f'3. download: FAILED ({type(e).__name__}: {e})')
    sys.exit(1)

match = 'OK' if dl_data == PAYLOAD else 'MISMATCH'
print(f'3. download: size={len(dl_data)}, match={match}')
if match != 'OK':
    sys.exit(1)

# 4. 隔离清理：开启 ISOLATED 时撤销本次提交，避免生产数据库残留测试条目。
if ISOLATED:
    try:
        req4 = urllib.request.Request(
            f'{BASE_URL}/api/item/unsubmit',
            data=urllib.parse.urlencode({'item_id': ITEM_ID, 'month': MONTH}).encode(),
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-Requested-With': 'XMLHttpRequest',
                'Cookie': 'auth_token=' + token,
            },
        )
        r4 = opener.open(req4, timeout=30)
        cleanup_resp = json.loads(r4.read().decode())
        print(f'4. cleanup: ok={cleanup_resp.get("ok")} (ISOLATED=1, 测试条目已自动撤销)')
    except Exception as e:
        # 清理失败不阻塞测试断言通过（已确认 upload/download OK），只警告
        print(f'4. cleanup: WARN ({type(e).__name__}: {e}); 残留数据请手动 /api/item/unsubmit')
else:
    print('4. cleanup: SKIP (ISOLATED=0, 保留提交供人工查看)')

print('=== ALL OK ===')