import sys, traceback
sys.path.insert(0, '.')
from app import app

with app.test_client() as c:
    # 用 admin token 登录
    rv = c.post('/api/auth/login', data={'username': 'admin', 'password': 'htglb888'})
    print('login:', rv.status_code, rv.get_json())
    token = rv.get_json()['token']
    # 模拟上传
    with open('_ctmp/test.xlsx', 'rb') as f:
        rv = c.post('/submit/item/2',
                    data={
                        'month': '2026-08',
                        'submission_type': 'file',
                        'submitter': '本地测试',
                        'remarks': 'probe',
                    },
                    content_type='multipart/form-data',
                    headers={'X-Auth-Token': token, 'X-Requested-With': 'XMLHttpRequest'},
                    buffered=True)
    print('upload:', rv.status_code, rv.content_type, len(rv.data))
    if rv.status_code >= 400:
        # 输出响应
        print(rv.data.decode('utf-8', errors='replace')[:2000])
