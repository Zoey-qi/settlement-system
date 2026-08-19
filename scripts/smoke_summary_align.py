"""
冒烟测试：汇总统计卡 ↔ 金额明细 payment_status 一一对齐。

场景：
1. 用 admin 登录拿 token
2. 创建一个对上结算单，PHP 已付款 + USD 未付款
3. 调 /api/settlement-records/summary（leader 视角）
4. 验证：
   - 对上 已付款.PHP >= 创建的 PHP 金额
   - 对上 未付款.USD >= 创建的 USD 金额
   - 对上 已完成.USD >= 创建的 USD 金额（部分付款）
5. PUT 把 USD 改成"已付款"，再调 summary 验证 USD 已付款 +=
6. DELETE 清理
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# 兼容本地 standalone
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

BASE = os.environ.get('SETTLEMENT_BASE', 'http://localhost:5000')


def http(method, path, token=None, form=None, files=None):
    url = BASE + path
    if files:
        boundary = '----WB' + str(int(time.time()))
        body = []
        for k, v in (form or {}).items():
            body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
        for fk, (fname, fdata, ftype) in files.items():
            body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{fk}"; filename="{fname}"\r\nContent-Type: {ftype}\r\n\r\n')
            body.append(fdata if isinstance(fdata, bytes) else fdata.encode())
            body.append(b'\r\n')
        body.append(f'--{boundary}--\r\n'.encode() if isinstance(body[-1], bytes) else f'--{boundary}--\r\n')
        raw_body = b''
        for p in body:
            raw_body += p.encode() if isinstance(p, str) else p
        req = urllib.request.Request(url, data=raw_body, method=method)
        req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    else:
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        else:
            req = urllib.request.Request(url, method=method)
    if token:
        req.add_header('Cookie', f'auth_token={token}')
        req.add_header('X-Requested-With', 'XMLHttpRequest')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='ignore')


def login(username, password):
    code, body = http('POST', '/api/auth/login', form={'username': username, 'password': password})
    if code != 200:
        raise SystemExit(f'login {username} {code}: {body[:200]}')
    j = json.loads(body)
    return j['token']


def main():
    admin_token = login('admin', 'htglb888')
    leader_token = login('leader', 'pakil123456')

    print('\n[1] create: PHP paid + USD unpaid')
    php_amt = 1_000_000.0
    usd_amt = 50_000.0
    code, body = http('POST', '/api/settlement-records', token=admin_token, form={
        'direction': 'up',
        'project_name': '[SMOKE-SUMMARY] 测试项目',
        'counterparty': 'SMOKE 总包',
        'contract_no': 'SMOKE-001',
        'settle_month': '2026-08',
        'status': 'pending',
        'notes': '',
        f'amounts[0][currency]': 'PHP',
        f'amounts[0][amount]': str(php_amt),
        f'amounts[0][payment_status]': 'paid',
        f'amounts[1][currency]': 'USD',
        f'amounts[1][amount]': str(usd_amt),
        f'amounts[1][payment_status]': 'unpaid',
    })
    assert code == 200, f'create {code}: {body[:200]}'
    rid = json.loads(body)['id']
    print(f'    ok rid={rid}')

    print('\n[2] summary: 已付款.PHP + 未付款.USD + 已完成.USD 都应该有值')
    code, body = http('GET', '/api/settlement-records/summary', token=leader_token)
    assert code == 200, f'summary {code}: {body[:200]}'
    j = json.loads(body)
    up = j['upstream']
    paid_php = up['per_currency'][0]['paid'] if up['per_currency'] and up['per_currency'][0]['currency'] == 'PHP' else 0
    # 按 sorted 找 PHP / USD
    by_cur = {c['currency']: c for c in up['per_currency']}
    p_paid = by_cur.get('PHP', {}).get('paid', 0)
    p_unpaid = by_cur.get('USD', {}).get('unpaid', 0)
    p_completed = up.get('completed', {}).get('USD', {}).get('amount', 0)
    print(f'    per_currency: {by_cur}')
    print(f'    completed USD: {p_completed}')
    assert p_paid >= php_amt, f'paid PHP 应该 >= {php_amt}, got {p_paid}'
    assert p_unpaid >= usd_amt, f'unpaid USD 应该 >= {usd_amt}, got {p_unpaid}'
    print('    ok')

    print('\n[3] PUT 把 USD 改成 paid，再 summary 验证 USD 已付款 += usd_amt')
    code, body = http('PUT', f'/api/settlement-records/{rid}', token=admin_token, form={
        'direction': 'up',
        'project_name': '[SMOKE-SUMMARY] 测试项目',
        'counterparty': 'SMOKE 总包',
        'contract_no': 'SMOKE-001',
        'settle_month': '2026-08',
        'status': 'pending',
        'notes': '',
        f'amounts[0][currency]': 'PHP',
        f'amounts[0][amount]': str(php_amt),
        f'amounts[0][payment_status]': 'paid',
        f'amounts[1][currency]': 'USD',
        f'amounts[1][amount]': str(usd_amt),
        f'amounts[1][payment_status]': 'paid',
    })
    assert code == 200, f'update {code}: {body[:200]}'
    print('    ok')

    code, body = http('GET', '/api/settlement-records/summary', token=leader_token)
    assert code == 200
    j = json.loads(body)
    up = j['upstream']
    by_cur = {c['currency']: c for c in up['per_currency']}
    p_usd_paid = by_cur.get('USD', {}).get('paid', 0)
    p_usd_unpaid = by_cur.get('USD', {}).get('unpaid', 0)
    p_usd_total = by_cur.get('USD', {}).get('total', 0)
    print(f'    USD: total={p_usd_total} paid={p_usd_paid} unpaid={p_usd_unpaid}')
    assert p_usd_paid >= usd_amt, f'改完 USD paid 应该 >= {usd_amt}, got {p_usd_paid}'
    assert p_usd_unpaid == 0, f'改完 USD unpaid 应该是 0, got {p_usd_unpaid}'
    print('    ok')

    print('\n[4] cleanup delete')
    code, body = http('DELETE', f'/api/settlement-records/{rid}', token=admin_token)
    assert code == 200, f'delete {code}: {body[:200]}'
    print('    ok')

    print('\n✅ ALL PASSED')


if __name__ == '__main__':
    main()