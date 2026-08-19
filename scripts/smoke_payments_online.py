"""线上付款追踪 smoke test
参考本地 scripts/smoke_payments.py 但用线上 URL + urllib
"""
import os
import sys
import json
import urllib.request
import urllib.parse
import ssl

ctx = ssl.create_default_context()
BASE = 'https://settlementsystem.vercel.app'


def http(method, path, token=None, data=None, is_form=True):
    url = BASE + path
    headers = {}
    if token:
        headers['X-Auth-Token'] = token
    if data is not None:
        if is_form:
            body = urllib.parse.urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            body = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
    else:
        body = None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=30, context=ctx)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# 1. 登录 admin
status, body = http('POST', '/api/auth/login',
                    data={'username': 'admin', 'password': 'htglb888'})
print(f'login: {status}')
assert status == 200, body[:200]
token = json.loads(body)['token']
print(f'token: {token[:20]}...')

# 2. 创建多币种结算单（带 [ISOLATED] 标记便于清理）
import datetime
stamp = datetime.datetime.now().strftime('%H%M%S')
form = {
    'direction': 'up',
    'project_name': f'[ISOLATED Smoke] Pakil Test {stamp}',
    'counterparty': 'Test Vendor',
    'contract_no': f'SMOKE-{stamp}',
    'settle_date': '2026-08-13',
    'status': 'pending',
    'notes': f'[合同管理部] ISOLATED smoke {stamp}',
    'amounts[0][currency]': 'PHP',
    'amounts[0][amount]': '100000',
    'amounts[1][currency]': 'USD',
    'amounts[1][amount]': '5000',
}
status, body = http('POST', '/api/settlement-records', token=token, data=form)
print(f'create: {status} {body[:200]}')
assert status == 200, f'create failed: {body[:300]}'
record_id = json.loads(body)['id']
print(f'record_id: {record_id}')

# 3. 列表 → 拿 amount_id
status, body = http('GET', '/api/settlement-records', token=token)
list_data = json.loads(body)
target = next((x for x in list_data.get('records', []) if x['id'] == record_id), None)
assert target is not None, f'record {record_id} not in list'
amount_ids = {a['currency']: a['id'] for a in target.get('amounts', [])}
print(f'amount_ids: {amount_ids}')
paid_summary = target.get('paid_summary', [])
print(f'initial paid_summary: {paid_summary}')
assert paid_summary[0]['paid'] == 0
assert paid_summary[0]['fully_paid'] is False

# 4. 部分付款 50000 PHP
pay_form = {
    'amount_id': str(amount_ids['PHP']),
    'currency': 'PHP',
    'amount': '50000',
    'payment_date': '2026-08-13',
    'payment_method': 'bank',
    'reference_no': f'SMOKE-{stamp}',
}
status, body = http('POST', f'/api/settlement-records/{record_id}/payments',
                    token=token, data=pay_form)
print(f'pay partial: {status} {body[:200]}')
assert status == 200, body[:300]

# 5. 再列表 → paid_summary 应有 PHP paid=50000, status=processing
status, body = http('GET', '/api/settlement-records', token=token)
target = next((x for x in json.loads(body).get('records', []) if x['id'] == record_id), None)
print(f'after pay: status={target.get("status")} paid_at={target.get("paid_at")}')
print(f'paid_summary: {target.get("paid_summary")}')
php_ps = next(p for p in target['paid_summary'] if p['currency'] == 'PHP')
assert php_ps['paid'] == 50000.0, f'expected 50000, got {php_ps["paid"]}'
assert target.get('status') == 'processing', f'expected processing, got {target.get("status")}'

# 6. 过付应被拒
overpay_form = {
    'amount_id': str(amount_ids['PHP']),
    'currency': 'PHP',
    'amount': '9999999',
    'payment_date': '2026-08-13',
}
status, body = http('POST', f'/api/settlement-records/{record_id}/payments',
                    token=token, data=overpay_form)
print(f'overpay: {status} {body[:300]}')
assert status == 400, 'overpay should be 400'

# 7. 临界过付 (50001) 也应被拒
overpay_form['amount'] = '50001'
status, body = http('POST', f'/api/settlement-records/{record_id}/payments',
                    token=token, data=overpay_form)
print(f'partial-overpay: {status} {body[:300]}')
assert status == 400, 'partial overpay should be 400'

# 8. 再付 50000 → 完全付清
pay_form['amount'] = '50000'
status, body = http('POST', f'/api/settlement-records/{record_id}/payments',
                    token=token, data=pay_form)
print(f'fully paid: {status} {body[:200]}')
assert status == 200

# 9. summary 接口验证
status, body = http('GET', '/api/settlement-records/summary', token=token)
sum_data = json.loads(body)
print(f'upstream per_currency count: {len(sum_data.get("upstream", {}).get("per_currency", []))}')

# 10. 清理（软删除测试 record）
status, body = http('DELETE', f'/api/settlement-records/{record_id}', token=token)
print(f'cleanup: {status} {body[:200]}')

# 11. 列出付款确认 voided 状态
status, body = http('GET', f'/api/settlement-records/{record_id}/payments', token=token)
print(f'payments after cleanup: {status}')

print('\n=== 线上 smoke 全部通过 ===')
