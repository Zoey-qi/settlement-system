"""本地验证：settlement_amounts.payment_status 字段从 PUT 到 GET 完整通路。

目标：验证用户编辑结算单时，每币种的付款状态（未付款/部分付款/已付款）
能被服务端持久化、列表接口能读回。
"""
import os
import sqlite3
import sys

# 让脚本能 import 同目录的 app.py / db.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 清掉旧 DB，从干净状态开始（用 sqlite 默认路径 data/settlement.db）
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'settlement.db')
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

from app import app, get_db, init_db, save_settlement_amounts, parse_amounts_from_form
from flask import json

# 自定义 sqlite3.Row 子类，支持 .get() 方法（DictCursor 风格）
class DictRow(sqlite3.Row):
    def get(self, key, default=None):
        try:
            return self[key]
        except (IndexError, KeyError):
            return default
sqlite3.Row = DictRow  # 影响 db.connect 内 conn.row_factory = sqlite3.Row

# 在 app context 里跑 init_db，让 init_schema 走完整的 Row → DictRow 路径
with app.app_context():
    init_db()

with app.test_client() as c:
    # 1) 登录
    rv = c.post('/api/auth/login',
                data={'username': 'admin', 'password': 'htglb888'},
                content_type='application/x-www-form-urlencoded')
    assert rv.status_code == 200, f'login failed: {rv.status_code}'
    tok = rv.get_json()['token']
    h = {'X-Auth-Token': tok}

    # 2) 创建含 2 个币种的结算单，且每个币种指定 payment_status
    rv = c.post('/api/settlement-records',
                data={
                    'direction': 'up',
                    'project_name': '多币种付款状态测试',
                    'counterparty': 'Test',
                    'settle_month': '2026-08',
                    'amounts[0][currency]': 'PHP',
                    'amounts[0][amount]': '100000',
                    'amounts[0][payment_status]': 'paid',
                    'amounts[1][currency]': 'USD',
                    'amounts[1][amount]': '5000',
                    'amounts[1][payment_status]': 'unpaid',
                },
                headers=h)
    assert rv.status_code == 200, f'create failed: {rv.status_code} {rv.data}'
    rid = rv.get_json()['id']
    print(f'[1] create record id={rid} OK')

    # 3) GET 列表 → amounts[i].payment_status 必须正确返回
    rv = c.get('/api/settlement-records', headers=h)
    body = rv.get_json()
    rows = body if isinstance(body, list) else body.get('records', body.get('items', []))
    target = next((r for r in rows if r.get('id') == rid), None)
    assert target is not None, f'record {rid} not in list response'
    assert len(target['amounts']) == 2, f'expected 2 amounts, got {len(target["amounts"])}'
    a0 = next(a for a in target['amounts'] if a['currency'] == 'PHP')
    a1 = next(a for a in target['amounts'] if a['currency'] == 'USD')
    assert a0['payment_status'] == 'paid', f'PHP payment_status expected paid, got {a0["payment_status"]}'
    assert a1['payment_status'] == 'unpaid', f'USD payment_status expected unpaid, got {a1["payment_status"]}'
    print(f'[2] list returns payment_status: PHP={a0["payment_status"]}, USD={a1["payment_status"]}')

    # 4) PUT 改 USD 为 partial
    rv = c.put(f'/api/settlement-records/{rid}',
               data={
                   'direction': 'up',
                   'project_name': '多币种付款状态测试',
                   'counterparty': 'Test',
                   'settle_month': '2026-08',
                   'amounts[0][currency]': 'PHP',
                   'amounts[0][amount]': '100000',
                   'amounts[0][payment_status]': 'paid',
                   'amounts[1][currency]': 'USD',
                   'amounts[1][amount]': '5000',
                   'amounts[1][payment_status]': 'partial',
               },
               headers=h)
    assert rv.status_code == 200, f'update failed: {rv.status_code} {rv.data}'
    print('[3] PUT USD payment_status=partial OK')

    # 5) 再次 GET 验证持久化
    rv = c.get('/api/settlement-records', headers=h)
    body = rv.get_json()
    rows = body if isinstance(body, list) else body.get('records', body.get('items', []))
    target = next((r for r in rows if r.get('id') == rid), None)
    a1 = next(a for a in target['amounts'] if a['currency'] == 'USD')
    assert a1['payment_status'] == 'partial', f'after PUT expected partial, got {a1["payment_status"]}'
    print(f'[4] after PUT USD payment_status={a1["payment_status"]} OK')

    # 6) 非法值回落
    rv = c.put(f'/api/settlement-records/{rid}',
               data={
                   'direction': 'up',
                   'project_name': '多币种付款状态测试',
                   'counterparty': 'Test',
                   'settle_month': '2026-08',
                   'amounts[0][currency]': 'PHP',
                   'amounts[0][amount]': '100000',
                   'amounts[0][payment_status]': 'INVALID_VALUE',
                   'amounts[1][currency]': 'USD',
                   'amounts[1][amount]': '5000',
                   'amounts[1][payment_status]': 'paid',
               },
               headers=h)
    assert rv.status_code == 200
    rv = c.get('/api/settlement-records', headers=h)
    body = rv.get_json()
    rows = body if isinstance(body, list) else body.get('records', body.get('items', []))
    target = next((r for r in rows if r.get('id') == rid), None)
    a0 = next(a for a in target['amounts'] if a['currency'] == 'PHP')
    assert a0['payment_status'] == 'unpaid', f'invalid should fall back to unpaid, got {a0["payment_status"]}'
    print(f'[5] invalid payment_status falls back to unpaid: PHP={a0["payment_status"]} OK')

    # 7) 旧客户端不传 status 时回落
    rv = c.put(f'/api/settlement-records/{rid}',
               data={
                   'direction': 'up',
                   'project_name': '多币种付款状态测试',
                   'counterparty': 'Test',
                   'settle_month': '2026-08',
                   'amounts[0][currency]': 'PHP',
                   'amounts[0][amount]': '100000',
                   # 无 payment_status
               },
               headers=h)
    assert rv.status_code == 200
    rv = c.get('/api/settlement-records', headers=h)
    body = rv.get_json()
    rows = body if isinstance(body, list) else body.get('records', body.get('items', []))
    target = next((r for r in rows if r.get('id') == rid), None)
    a0 = next(a for a in target['amounts'] if a['currency'] == 'PHP')
    assert a0['payment_status'] == 'unpaid'
    print(f'[6] missing payment_status falls back to unpaid OK')

    # 8) 清理
    rv = c.delete(f'/api/settlement-records/{rid}', headers=h)
    print(f'[7] cleanup status={rv.status_code}')

print('\n=== amount status 通路验证全部通过 ===')