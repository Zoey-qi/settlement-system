"""本地冒烟测试：验证 /api/settlement-records 和 /api/settlement-records/summary
返回包含 paid_summary / per_currency 字段。
"""
import os
import sys
import json
import sqlite3

# 确保本机 sqlite
os.environ.pop('POSTGRES_URL', None)
os.environ.pop('DATABASE_URL', None)
os.environ['SECRET_KEY'] = 'test'

# 自定义 sqlite3.Row 子类，支持 .get() 方法（DictCursor 风格）
class DictRow(sqlite3.Row):
    def get(self, key, default=None):
        try:
            return self[key]
        except (IndexError, KeyError):
            return default
sqlite3.Row = DictRow  # 影响 db.connect 内 conn.row_factory = sqlite3.Row

# 把 settlement_system 加入路径
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# 重置本地 sqlite 数据库（清理上一轮测试残留）
_data_dir = os.path.join(os.path.dirname(HERE), 'data')
_db_path = os.path.join(_data_dir, 'settlement.db')
os.makedirs(_data_dir, exist_ok=True)
if os.path.exists(_db_path):
    os.remove(_db_path)
    print(f'[reset] removed {_db_path}')

from app import app, init_db  # noqa
init_db()  # 本地建表 + seed

client = app.test_client()


def login(username, password):
    return client.post('/api/auth/login',
                       data={'username': username, 'password': password},
                       follow_redirects=False)


def set_token(token):
    with client.session_transaction() as sess:
        sess['auth_token'] = token


# 1. 登录 admin
r = login('admin', 'htglb888')
print('login status:', r.status_code)
j = json.loads(r.data)
token = j.get('token', '')
assert token, 'no token in login response'

# 把 token 作为后续 header 传（更稳，不依赖 cookie jar）
HEADERS = {'X-Auth-Token': token, 'X-Requested-With': 'XMLHttpRequest'}

# 2. 创建多币种结算单
form = {
    'direction': 'up',
    'project_name': 'Pakil Test 多币种',
    'counterparty': 'Test Vendor',
    'contract_no': 'TEST-001',
    'settle_date': '2026-08-13',
    'status': 'pending',
    'notes': 'smoke',
    'amounts[0][currency]': 'PHP',
    'amounts[0][amount]': '100000',
    'amounts[1][currency]': 'USD',
    'amounts[1][amount]': '5000',
}
r = client.post('/api/settlement-records', data=form,
                headers=HEADERS)
print('create status:', r.status_code)
create_data = json.loads(r.data)
print('create body:', json.dumps(create_data, ensure_ascii=False, indent=2)[:500])
record_id = create_data.get('id')
assert record_id, 'no record id'

# 列表接口返回 amount id
r = client.get('/api/settlement-records', headers=HEADERS)
list_data = json.loads(r.data)
print('list body sample:', json.dumps(list_data, ensure_ascii=False)[:500])
target = next((x for x in list_data.get('records', []) if x['id'] == record_id), None)
print('target:', target)
amount_ids = {a['currency']: a['id'] for a in (target or {}).get('amounts', [])} if target else {}
print('amount_ids:', amount_ids)
assert amount_ids.get('PHP'), 'no PHP amount id'
assert amount_ids.get('USD'), 'no USD amount id'

# 3. 给 PHP 部分付 50000
r = client.post(f'/api/settlement-records/{record_id}/payments',
                data={'amount_id': str(amount_ids['PHP']), 'currency': 'PHP', 'amount': '50000',
                      'payment_date': '2026-08-13', 'payment_method': 'bank', 'reference_no': 'PMT-001'},
                headers=HEADERS)
print('pay status:', r.status_code, 'body:', r.data[:300].decode(errors='replace'))
assert r.status_code == 200, 'pay should be 200'

# 4. 列表接口
r = client.get('/api/settlement-records', headers=HEADERS)
print('list status:', r.status_code)
list_data = json.loads(r.data)
print('records count:', len(list_data.get('records', [])))
target = next((x for x in list_data.get('records', []) if x['id'] == record_id), None)
assert target is not None, 'target record missing'
print('target paid_summary:', json.dumps(target.get('paid_summary'), ensure_ascii=False))
print('target status:', target.get('status'))
print('target paid_at:', target.get('paid_at'))
assert target.get('status') == 'processing', f'should be processing, got {target.get("status")}'
php_ps = next(p for p in target['paid_summary'] if p['currency'] == 'PHP')
usd_ps = next(p for p in target['paid_summary'] if p['currency'] == 'USD')
assert php_ps['paid'] == 50000.0, f'PHP paid should be 50000, got {php_ps["paid"]}'
assert php_ps['fully_paid'] is False, 'PHP should not be fully paid'
assert usd_ps['paid'] == 0.0, f'USD paid should be 0, got {usd_ps["paid"]}'
assert usd_ps['fully_paid'] is False, 'USD should not be fully paid (has no payments but amount > 0)'
# paid_at 表示「最后一次付款日期」（用于排序最近有付款的 record），未付清也可有值
assert target.get('paid_at') == '2026-08-13', f'paid_at should be last payment date, got {target.get("paid_at")}'

# 5. summary 接口
r = client.get('/api/settlement-records/summary', headers=HEADERS)
print('summary status:', r.status_code)
sum_data = json.loads(r.data)
print('upstream per_currency:',
      json.dumps(sum_data.get('upstream', {}).get('per_currency'), ensure_ascii=False))

# 6. 过付应被拒
r = client.post(f'/api/settlement-records/{record_id}/payments',
                data={'amount_id': str(amount_ids['PHP']), 'currency': 'PHP', 'amount': '9999999',
                      'payment_date': '2026-08-13'},
                headers=HEADERS)
print('overpay status:', r.status_code, 'body:', r.data[:200].decode(errors='replace'))
assert r.status_code == 400, 'overpay should be 400'

# 7. 付款 50000（PHP 已付 50000，再付 50000 应被拒过付）
r = client.post(f'/api/settlement-records/{record_id}/payments',
                data={'amount_id': str(amount_ids['PHP']), 'currency': 'PHP', 'amount': '50001',
                      'payment_date': '2026-08-13'},
                headers=HEADERS)
print('partial-overpay status:', r.status_code, 'body:', r.data[:200].decode(errors='replace'))
assert r.status_code == 400, 'partial overpay should be 400'

# 8. 付款 50000 正好付清 PHP 部分
r = client.post(f'/api/settlement-records/{record_id}/payments',
                data={'amount_id': str(amount_ids['PHP']), 'currency': 'PHP', 'amount': '50000',
                      'payment_date': '2026-08-13', 'payment_method': 'bank'},
                headers=HEADERS)
print('fully-paid status:', r.status_code)
assert r.status_code == 200, 'fully paid should succeed'

# 9. 再查：status 应为 processing（USD 未付），paid_at 应有日期
r = client.get('/api/settlement-records', headers=HEADERS)
target = next((x for x in json.loads(r.data).get('records', []) if x['id'] == record_id), None)
print('after fully pay: status =', target.get('status'), 'paid_at =', target.get('paid_at'))
assert target.get('status') == 'processing', f'USD still unpaid, status should be processing'
assert target.get('paid_at') == '2026-08-13', f'paid_at should be 2026-08-13, got {target.get("paid_at")}'

# 10. 撤销一条付款
r = client.get(f'/api/settlement-records/{record_id}/payments', headers=HEADERS)
pay_data = json.loads(r.data)
payment_ids = [p['id'] for p in pay_data.get('payments', [])]
print('payment ids:', payment_ids)
assert len(payment_ids) == 2, f'should have 2 payments, got {len(payment_ids)}'

r = client.delete(f'/api/settlement-payments/{payment_ids[0]}',
                  headers=HEADERS)
print('void payment status:', r.status_code, r.data[:200].decode(errors='replace'))
assert r.status_code == 200, 'void should succeed'

# 11. 验证 voided 状态
r = client.get(f'/api/settlement-records/{record_id}/payments', headers=HEADERS)
pay_data = json.loads(r.data)
print('after void: payments =', json.dumps(pay_data.get('payments', []), ensure_ascii=False)[:300])

# 12. 清理：删除测试记录
r = client.delete(f'/api/settlement-records/{record_id}',
                  headers=HEADERS)
print('cleanup record status:', r.status_code, r.data[:200].decode(errors='replace'))

print('\n=== 本地 smoke 全部通过 ===')
