# -*- coding: utf-8 -*-
"""
菲律宾帕基尔抽蓄基础处理工程项目 - 月度结算数据管理系统
Flask Application
支持条目级独立提交、文字编辑、模板关联下载
"""

import os
import re
import sqlite3
import hashlib
import shutil
import io
import secrets
import time
import json
import urllib.request
import urllib.error
from urllib.parse import quote
from datetime import datetime, date
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, send_file, flash, g, abort
)
from werkzeug.utils import secure_filename
from db import connect, USE_POSTGRES, init_schema, seed_default_data, insert_returning_id
import mimetypes
mimetypes.add_type('application/manifest+json', '.webmanifest')

# ===========================================================================
# 配置
# ===========================================================================
IS_VERCEL = bool(os.environ.get('VERCEL'))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if IS_VERCEL:
    # Vercel 文件系统只读，用 /tmp
    UPLOAD_DIR = '/tmp/uploads'
    TEMPLATE_DIR = '/tmp/template_files'
    DATA_DIR = '/tmp/data'
else:
    UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
    TEMPLATE_DIR = os.path.join(BASE_DIR, 'template_files')
    DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'settlement.db')

if not USE_POSTGRES:
    for d in [UPLOAD_DIR, TEMPLATE_DIR, DATA_DIR]:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass  # 只读文件系统，忽略

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config['SECRET_KEY'] = 'pakil-settlement-2025'
# Vercel Hobby 限制请求体 4.5MB；本地无限制
# 注：单次请求 4MB 仍较小，但够用。若需要大文件可改为流式上传（Vercel 受 serverless body 限制）。
if USE_POSTGRES:
    app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024  # 4MB (Vercel safe，含 PDF 等)
else:
    app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB (local)
app.config['JSON_AS_ASCII'] = False
# 静态资源文件名稳定，允许浏览器/CDN 缓存 1 天，提升加载速度（仅影响 /static/*，不影响 HTML/API）
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400
if not USE_POSTGRES:
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True

ALLOWED_EXTENSIONS = {
    '.xlsx', '.xls', '.csv', '.doc', '.docx',
    '.pdf', '.jpg', '.jpeg', '.png', '.zip', '.rar',
    '.ppt', '.pptx', '.txt'
}

# ===========================================================================
# Vercel Blob 对象存储（可选，启用后附件上传绕开 Neon BYTEA，显著提升上传速度）
# 仅在环境变量 BLOB_READ_WRITE_TOKEN 存在时启用；缺失则自动回退到原 DB/磁盘存储。
# ===========================================================================
BLOB_TOKEN = os.environ.get('BLOB_READ_WRITE_TOKEN')
BLOB_ACCESS = os.environ.get('BLOB_ACCESS', 'public')  # public 或 private


def blob_enabled():
    """是否在 Vercel Blob 启用状态下（有读写令牌）"""
    return bool(BLOB_TOKEN)


def _blob_store_id():
    """从 BLOB_READ_WRITE_TOKEN 解析 store id。

    真实令牌格式为 vercel_blob_rw_<storeId>（4 段），storeId 取
    token.split('_')[3]，与官方 @vercel/blob SDK 的
    parseStoreIdFromReadWriteToken 完全一致。
    """
    if not BLOB_TOKEN:
        return None
    parts = BLOB_TOKEN.split('_')
    if len(parts) < 4:
        return None
    return parts[3]


def blob_put_bytes(file_bytes, filename, folder):
    """把字节上传到 Vercel Blob，返回 (url, pathname)。失败抛异常。

    服务端直传：走 Blob 控制面 https://vercel.com/api/blob（与官方
    @vercel/blob SDK 的 put() 一致）——完整读写令牌作为 Bearer，
    store id 作为 x-vercel-blob-store-id 头；*.blob.vercel-storage.com
    那个域名仅用于下载，不是上传端点。
    """
    store_id = _blob_store_id()
    if not store_id:
        raise ValueError('无法解析 BLOB store id（令牌格式应为 vercel_blob_rw_<storeId>）')
    safe = secure_filename(filename) or 'file'
    ts = datetime.now().strftime('%Y%m%d%H%M%S%f')
    pathname = f"{folder}/{ts}_{safe}"
    api_base = os.environ.get('VERCEL_BLOB_API_URL') or 'https://vercel.com/api/blob'
    put_url = f"{api_base}/?pathname={quote(pathname)}"
    ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    req = urllib.request.Request(put_url, data=file_bytes, method='PUT')
    req.add_header('authorization', f'Bearer {BLOB_TOKEN}')
    req.add_header('x-vercel-blob-store-id', store_id)
    req.add_header('x-api-version', '12')
    req.add_header('x-vercel-blob-access', BLOB_ACCESS)
    req.add_header('content-type', ctype)
    req.add_header('x-content-type', ctype)
    req.add_header('x-add-random-suffix', '0')
    req.add_header('x-api-blob-request-id',
                  f"{store_id}:{int(time.time())}:{os.urandom(4).hex()}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode('utf-8'))
    return (body.get('url') or f"https://{store_id}.{BLOB_ACCESS}.blob.vercel-storage.com/{pathname}",
            body.get('pathname') or pathname)


@app.route('/api/blob-status')
def api_blob_status():
    """诊断 Vercel Blob 是否真正可用：做真实上传 + 删除往返。

    未配置令牌时仅报告状态；已配置时实测一次上传并清理，便于激活后
    一键验证（访问 /api/blob-status 即可看到 roundtrip 结果）。
    """
    # 权限检查放在函数体内（运行时 require_role/get_current_user 均已定义），
    # 避免模块加载期装饰器引用未定义符号导致全站 500。
    user = get_current_user()
    if not user or user.get('role') != 'admin':
        return jsonify({'error': '权限不足'}), 403
    if not blob_enabled():
        return jsonify({'enabled': False,
                        'reason': 'BLOB_READ_WRITE_TOKEN 未设置，当前走原存储回退'})
    store_id = _blob_store_id()
    result = {'enabled': True, 'store_id': store_id, 'access': BLOB_ACCESS}
    try:
        url, pathname = blob_put_bytes(b'vercel-blob-self-test', 'self_test.txt', 'blob-test')
        result['upload'] = {'ok': True, 'url': url, 'pathname': pathname}
        # 清理刚上传的测试文件（走 /delete 控制面）
        api_base = os.environ.get('VERCEL_BLOB_API_URL') or 'https://vercel.com/api/blob'
        del_url = f"{api_base}/delete"
        dreq = urllib.request.Request(
            del_url, data=json.dumps({'urls': [url]}).encode('utf-8'), method='POST')
        dreq.add_header('authorization', f'Bearer {BLOB_TOKEN}')
        dreq.add_header('x-vercel-blob-store-id', store_id)
        dreq.add_header('x-api-version', '12')
        dreq.add_header('content-type', 'application/json')
        with urllib.request.urlopen(dreq, timeout=30) as dresp:
            dbody = json.loads(dresp.read().decode('utf-8'))
        result['delete'] = {'ok': True, 'response': dbody}
        result['roundtrip'] = 'OK'
    except Exception as e:
        result['roundtrip'] = 'FAIL'
        result['error'] = f'{type(e).__name__}: {e}'
    return jsonify(result)


# ===========================================================================
# 权限装饰器与辅助函数（必须定义在所有路由之前）
# ===========================================================================
def require_auth(f):
    """装饰器：要求登录。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.path.startswith('/api/'):
                return jsonify({'error': '未登录或会话已过期'}), 401
            return redirect(url_for('login_page', next=request.path))
        g.current_user = user
        return f(*args, **kwargs)
    return wrapper


def require_role(*roles):
    """装饰器：要求指定角色之一。"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if not user:
                if request.path.startswith('/api/'):
                    return jsonify({'error': '未登录'}), 401
                return redirect(url_for('login_page'))
            if user['role'] not in roles:
                if request.path.startswith('/api/'):
                    return jsonify({'error': '权限不足'}), 403
                flash('权限不足，无法访问该页面', 'danger')
                return redirect(url_for('dashboard'))
            g.current_user = user
            return f(*args, **kwargs)
        return wrapper
    return decorator


def user_can_write_to_dept(user, dept_name):
    """判断当前用户是否有权对指定部门进行写操作。

    权限矩阵：
      - admin    → 所有部门
      - liaison  → 仅本部门
      - director → 仅本部门只读（不允许写）
      - leader   → 完全不允许写
    """
    if not user:
        return False
    role = user['role']
    if role == 'admin':
        return True
    if role == 'liaison':
        return dept_name == user.get('department', '')
    return False


def _check_task_dept_or_403(task_config_id):
    """根据 task_config_id 查询对应部门名，供路由层做权限校验。"""
    db = get_db()
    row = db.execute('''
        SELECT tc.id, d.name as dept_name
        FROM task_configs tc
        JOIN departments d ON tc.department_id = d.id
        WHERE tc.id = ?
    ''', (task_config_id,)).fetchone()
    if not row:
        return db, None
    return db, row['dept_name']


# ===========================================================================
# 数据库
# ===========================================================================
def get_db():
    if 'db' not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def split_materials(materials_text):
    """将材料描述文本智能拆分为独立条目，处理括号内的顿号"""
    if not materials_text:
        return []
    items = []
    current = ''
    depth = 0
    for char in materials_text:
        if char in '（(':
            depth += 1
            current += char
        elif char in '）)':
            depth = max(0, depth - 1)
            current += char
        elif char in '、\n;；' and depth == 0:
            if current.strip():
                items.append(current.strip())
            current = ''
        else:
            current += char
    if current.strip():
        items.append(current.strip())
    return items


def init_db():
    """初始化数据库并预填充数据"""
    conn = connect()
    init_schema(conn)
    seed_default_data(conn)
    seed_users(conn)  # 新增：初始化登录用户

    # 迁移：将已有 task_configs 拆分为 task_items
    migrate_to_items(conn)

    conn.close()


def migrate_to_items(conn):
    """将已有的 task_configs.required_materials 拆分为 task_items 条目。

    关键约束：仅对 items_initialized=0 的配置建档；建档后立即标记该配置为已初始化。
    此举修复『删除的条目又自己加回来』——用户删除某配置的全部条目后，该配置
    items_initialized 已为 1，冷启动不再重建其条目。新增配置（DEFAULT 0）仍会在
    首次冷启动被建档一次。
    """
    configs = conn.execute('SELECT * FROM task_configs').fetchall()
    for cfg in configs:
        # 已初始化（含用户删除后变空的配置）：跳过，绝不重建条目
        if cfg.get('items_initialized'):
            continue
        # 已有条目但标记未置位（其它路径创建）：直接标记并跳过，避免重复建档
        existing = conn.execute('SELECT COUNT(*) as c FROM task_items WHERE task_config_id = ?', (cfg['id'],)).fetchone()['c']
        if existing > 0:
            conn.execute('UPDATE task_configs SET items_initialized = 1 WHERE id = ?', (cfg['id'],))
            continue
        items = split_materials(cfg['required_materials'])
        if not items:
            # 如果拆分后为空，创建一个默认条目
            items = [cfg['required_materials'] or '需提交资料']
        for idx, item_name in enumerate(items):
            conn.execute('''
                INSERT INTO task_items (task_config_id, item_name, sort_order, is_active)
                VALUES (?,?,?,1)
            ''', (cfg['id'], item_name, idx))
        conn.execute('UPDATE task_configs SET items_initialized = 1 WHERE id = ?', (cfg['id'],))
    conn.commit()

    # 自动关联模板
    auto_link_templates(conn)


def auto_link_templates(conn):
    """根据名称匹配自动关联条目与模板文件"""
    items = conn.execute('SELECT * FROM task_items WHERE template_file_id IS NULL').fetchall()
    templates = conn.execute('SELECT * FROM template_files').fetchall()
    for item in items:
        item_name = item['item_name']
        best_match = None
        best_score = 0
        for tpl in templates:
            tpl_name = tpl['name']
            # 精确包含匹配
            if tpl_name in item_name or item_name in tpl_name:
                score = len(tpl_name) + len(item_name)
                if score > best_score:
                    best_score = score
                    best_match = tpl
            # 关键词匹配
            elif any(kw in item_name and kw in tpl_name for kw in ['罚款', '扣款', '工资', '考勤', '台账', '工程量', '验收', '质量', '安全', '材料', '领用', '后勤', '水电', '食堂']):
                score = 10
                if score > best_score:
                    best_score = score
                    best_match = tpl
        if best_match:
            conn.execute('UPDATE task_items SET template_file_id = ? WHERE id = ?', (best_match['id'], item['id']))
    conn.commit()


def ensure_tasks_exist(month_str):
    """确保某月所有活跃任务配置对应的任务行已存在（批量，1 次查询）。

    仅做 INSERT ... ON CONFLICT DO NOTHING 建行，不重算状态：
    - 各展示页（仪表盘/提交列表/汇总）依据条目提交情况内联计算状态；
    - 提交/撤销/删除单条后由 ensure_tasks_for_month(only_config_id=...) 精准更新该配置状态。
    该 SQL 在 SQLite 与 PostgreSQL 下均合法（upsert 语法两边通用）。
    """
    db = get_db()
    db.execute('''
        INSERT INTO tasks (task_config_id, month, department_id, settlement_type_id, required_materials, deadline_day, status)
        SELECT id, ?, department_id, settlement_type_id, required_materials, deadline_day, 'pending'
        FROM task_configs
        WHERE is_active = 1
        ON CONFLICT (task_config_id, month) DO NOTHING
    ''', (month_str,))
    db.commit()


def ensure_tasks_for_month(month_str, only_config_id=None):
    """确保某月任务行存在，并按需刷新状态。

    - only_config_id 给定：只重算该任务配置（提交/撤销/删除单条后调用，减少 Neon 查询往返）；
    - 不给定：重算全部（仅兼容/修复场景，常规展示页请勿再调用全量）。
    """
    ensure_tasks_exist(month_str)
    db = get_db()
    if only_config_id is not None:
        status = compute_task_status(only_config_id, month_str, db)
        task = db.execute('SELECT id FROM tasks WHERE task_config_id=? AND month=?', (only_config_id, month_str)).fetchone()
        if task:
            db.execute('UPDATE tasks SET status=? WHERE id=?', (status, task['id']))
        db.commit()
        return

    # 全量重算（兼容/修复用，不应出现在常规展示路径）
    configs = db.execute('SELECT id FROM task_configs WHERE is_active = 1').fetchall()
    for cfg in configs:
        status = compute_task_status(cfg['id'], month_str, db)
        task = db.execute('SELECT id FROM tasks WHERE task_config_id=? AND month=?', (cfg['id'], month_str)).fetchone()
        if task:
            db.execute('UPDATE tasks SET status=? WHERE id=?', (status, task['id']))
    db.commit()


def compute_task_status(task_config_id, month, db=None):
    """根据条目提交情况计算任务状态"""
    if db is None:
        db = get_db()
    items = db.execute('SELECT id FROM task_items WHERE task_config_id = ? AND is_active = 1', (task_config_id,)).fetchall()
    if not items:
        return 'pending'
    total = len(items)
    item_ids = [i['id'] for i in items]
    placeholders = ','.join('?' * len(item_ids))
    completed = db.execute(
        f'SELECT COUNT(*) as c FROM item_submissions WHERE task_item_id IN ({placeholders}) AND month = ?',
        item_ids + [month]
    ).fetchone()['c']
    if completed >= total:
        return 'completed'
    elif completed > 0:
        return 'partial'
    else:
        return 'pending'


def get_items_with_status(task_config_id, month):
    """获取某任务配置下所有条目及其月度提交状态"""
    db = get_db()
    items = db.execute('''
        SELECT ti.*,
               ist.id as sub_id, ist.submission_type, ist.file_name as sub_file_name,
               ist.stored_name as sub_stored_name, ist.file_size as sub_file_size,
               ist.submitter, ist.submitted_at, ist.remarks as sub_remarks,
               tf.id as tpl_id, tf.name as tpl_name, tf.stored_name as tpl_stored,
               tf.file_name as tpl_file_name
        FROM task_items ti
        LEFT JOIN item_submissions ist ON ist.task_item_id = ti.id AND ist.month = ?
        LEFT JOIN template_files tf ON tf.id = ti.template_file_id
        WHERE ti.task_config_id = ? AND ti.is_active = 1
        ORDER BY ti.sort_order, ti.id
    ''', (month, task_config_id)).fetchall()
    return items


def get_items_with_status_bulk(task_config_ids, month):
    """批量获取多个任务配置下所有条目及其月度提交状态（消除 N+1）。

    一次 IN 查询替代逐个 task_config 调用 get_items_with_status，
    在 Vercel+Neon 架构下把约 14+ 次网络往返降为 1 次。
    返回 {task_config_id: [item, ...]}。
    """
    if not task_config_ids:
        return {}
    db = get_db()
    placeholders = ','.join('?' * len(task_config_ids))
    items = db.execute(f'''
        SELECT ti.*,
               ist.id as sub_id, ist.submission_type, ist.file_name as sub_file_name,
               ist.stored_name as sub_stored_name, ist.file_size as sub_file_size,
               ist.submitter, ist.submitted_at, ist.remarks as sub_remarks,
               tf.id as tpl_id, tf.name as tpl_name, tf.stored_name as tpl_stored,
               tf.file_name as tpl_file_name
        FROM task_items ti
        LEFT JOIN item_submissions ist ON ist.task_item_id = ti.id AND ist.month = ?
        LEFT JOIN template_files tf ON tf.id = ti.template_file_id
        WHERE ti.task_config_id IN ({placeholders}) AND ti.is_active = 1
        ORDER BY ti.task_config_id, ti.sort_order, ti.id
    ''', [month] + list(task_config_ids)).fetchall()
    result = {}
    for it in items:
        result.setdefault(it['task_config_id'], []).append(it)
    return result


def get_current_month():
    return datetime.now().strftime('%Y-%m')


def parse_month(month_str):
    try:
        parts = month_str.split('-')
        return int(parts[0]), int(parts[1])
    except Exception:
        return None, None


def is_overdue(task, today=None):
    if task['status'] == 'completed':
        return False
    if today is None:
        today = date.today()
    year, month = parse_month(task['month'])
    if year is None:
        return False
    deadline_day = task['deadline_day']
    try:
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        actual_deadline_day = min(deadline_day, last_day)
        deadline_date = date(year, month, actual_deadline_day)
        return today > deadline_date
    except Exception:
        return False


def allowed_file(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def request_is_ajax():
    """判断是否为 AJAX 请求（前端 fetch 带 X-Requested-With 头）"""
    return request.headers.get('X-Requested-With', '').lower() == 'xmlhttprequest'


def save_upload_file(file, subdir='item_submissions'):
    """保存上传文件。

    返回 6 元组 (stored_name, file_size, original, file_data, blob_url, blob_pathname)。
    - 未启用 Blob：file_data 为字节（Vercel）/ None（本地磁盘），blob 两字段为 None。
    - 启用 Blob：file_data 置 None，blob 两字段为对象存储 URL/pathname。
    任何异常（网络/令牌错误）都回退到原 DB/磁盘存储，保证上传不中断。
    """
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    original = secure_filename(file.filename)
    if not original:
        original = 'unnamed_file'
    stored_name = f"{timestamp}_{original}"

    # 尝试走 Vercel Blob（更快、不撑大数据库）；失败则回退
    if blob_enabled():
        try:
            file_data = file.read()
            file_size = len(file_data)
            blob_url, blob_pathname = blob_put_bytes(file_data, original, subdir)
            return stored_name, file_size, original, None, blob_url, blob_pathname
        except Exception as e:
            app.logger.warning(f'[blob] 上传失败，回退到原存储: {e}')

    if USE_POSTGRES:
        # Vercel: 读取文件到内存
        file_data = file.read()
        file_size = len(file_data)
        return stored_name, file_size, original, file_data, None, None
    else:
        # 本地: 存到磁盘
        upload_dir = os.path.join(UPLOAD_DIR, subdir)
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, stored_name)
        file.save(filepath)
        file_size = os.path.getsize(filepath)
        return stored_name, file_size, original, None, None, None


# ===========================================================================
# 路由 - 页面
# ===========================================================================
# 公开路由（无需登录）：登录页 / 登录接口 / 健康检查 / 静态资源
# 除此之外的所有路由全部需要登录态（@require_auth 装饰器统一处理）
PUBLIC_ENDPOINTS = {
    'login_page',           # /login
    'api_auth_login',       # POST /api/auth/login
    'health_check',         # /health
    'static',               # /static/<path>
    'api_auth_departments', # /api/auth/departments（登录页部门下拉用）
    'api_auth_logout',      # POST /api/auth/logout（清除 token，登出）
    'api_auth_logout_ui',   # POST /api/auth/logout-ui（表单退出，跳登录页）
}


@app.before_request
def enforce_login():
    """全局拦截：未登录访问任何非公开路由 → 跳 /login

    - 配合 require_auth / require_role 实现"打开网站就是登录页"
    - 公开路由（login / auth/login / health / static / 部门下拉）仍可访问
    """
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    user = get_current_user()
    if user:
        g.current_user = user
        return None
    # /api/* 未登录返回 401 JSON，其余页面跳登录
    if request.path.startswith('/api/'):
        return jsonify({'error': '未登录或会话已过期'}), 401
    return redirect(url_for('login_page', next=request.path))


@app.before_request
def inject_current_user():
    """全局注入当前登录用户，供 base.html 导航栏使用。
    未登录时设置一个匿名对象，避免模板出现 UndefinedError。
    同时初始化 g.requested_icons = set()，模板 ui.icon() 宏会往里加 name，
    base.html 末尾按需输出 <symbol>——无需每个请求多次 lazy load。
    """
    user = get_current_user()
    if user:
        g.current_user = user
    else:
        # 匿名占位对象：role=None 使任何角色判断都为假
        class _Anon:
            role = None
            display_name = ''
            department = ''
        g.current_user = _Anon()
    # ui.icon() 宏需要此 set；登录页 block scripts 中也用 <use href="#i-...">，
    # 那些 icon 名同样会被 ui.icon() 收集到（同一个模板）。
    g.requested_icons = set()


@app.route('/')
@require_auth
def dashboard():
    month = request.args.get('month', get_current_month())
    ensure_tasks_exist(month)

    db = get_db()
    user = g.current_user
    user_role = user['role']
    user_dept = user.get('department', '') if user_role in ('director', 'liaison') else None

    # --- 1. 取所有任务（包含部门关联） ---
    tasks = db.execute('''
        SELECT t.*, d.name as dept_name, d.contact_person, d.sort_order, d.id as dept_id,
               st.code as st_code, st.name as st_name,
               tc.id as task_config_id, tc.remarks as config_remarks,
               tc.deadline_day as config_deadline_day
        FROM tasks t
        JOIN departments d ON t.department_id = d.id
        JOIN settlement_types st ON t.settlement_type_id = st.id
        JOIN task_configs tc ON t.task_config_id = tc.id
        WHERE t.month = ?
        ORDER BY st.id, d.sort_order
    ''', (month,)).fetchall()

    # 为每个任务附加条目信息（批量查询所有配置条目，避免 N+1）
    config_ids = [t['task_config_id'] for t in tasks]
    items_by_config = get_items_with_status_bulk(config_ids, month)
    task_list = []
    for t in tasks:
        items = items_by_config.get(t['task_config_id'], [])
        total_items = len(items)
        completed_items = sum(1 for i in items if i['sub_id'])
        task_dict = dict(t)
        task_dict['deadline_day'] = t['config_deadline_day']
        task_dict['items'] = items
        task_dict['total_items'] = total_items
        task_dict['completed_items'] = completed_items
        # 状态已从 get_items_with_status 的结果内联得出，避免再发一次 COUNT 查询
        if total_items and completed_items >= total_items:
            task_dict['status'] = 'completed'
        elif completed_items > 0:
            task_dict['status'] = 'partial'
        else:
            task_dict['status'] = 'pending'
        task_list.append(task_dict)

    # 过滤掉没有任何条目（部门尚未配置资料）的任务，避免空任务占据列表与统计
    task_list = [t for t in task_list if t['total_items'] > 0]

    total = len(task_list)
    completed = sum(1 for t in task_list if t['status'] == 'completed')
    pending = sum(1 for t in task_list if t['status'] == 'pending')
    partial = sum(1 for t in task_list if t['status'] == 'partial')
    overdue = sum(1 for t in task_list if is_overdue(t))

    upstream_tasks = [t for t in task_list if t['st_code'] == 'upstream']
    downstream_tasks = [t for t in task_list if t['st_code'] == 'downstream']

    # --- 2. 各部门完成情况汇总（用于"相互激励"） ---
    # 计算每个部门的：总任务数、已完成、未完成、完成率、排名分
    dept_stats = {}
    for t in task_list:
        dn = t['dept_name']
        if dn not in dept_stats:
            dept_stats[dn] = {
                'dept_name': dn,
                'sort_order': t['sort_order'] or 999,
                'total': 0, 'completed': 0, 'partial': 0, 'pending': 0, 'overdue': 0,
                'upstream_total': 0, 'upstream_completed': 0,
                'downstream_total': 0, 'downstream_completed': 0,
            }
        s = dept_stats[dn]
        s['total'] += 1
        if t['status'] == 'completed':
            s['completed'] += 1
        elif t['status'] == 'partial':
            s['partial'] += 1
        else:
            s['pending'] += 1
        if is_overdue(t):
            s['overdue'] += 1
        if t['st_code'] == 'upstream':
            s['upstream_total'] += 1
            if t['status'] == 'completed':
                s['upstream_completed'] += 1
        elif t['st_code'] == 'downstream':
            s['downstream_total'] += 1
            if t['status'] == 'completed':
                s['downstream_completed'] += 1

    # 计算完成率 + 排名
    for dn, s in dept_stats.items():
        s['rate'] = round(s['completed'] / s['total'] * 100, 1) if s['total'] > 0 else 0.0

    # 按完成率降序
    dept_ranking = sorted(dept_stats.values(), key=lambda x: (-x['rate'], x['sort_order']))
    # 标 rank
    for i, s in enumerate(dept_ranking, 1):
        s['rank'] = i
        s['is_top'] = (i == 1 and s['rate'] > 0)
        s['is_my_dept'] = (dn == user_dept) if user_dept else False
    # 修复 is_my_dept 标注
    for s in dept_ranking:
        s['is_my_dept'] = (s['dept_name'] == user_dept) if user_dept else False

    # --- 3. 视图模式：self 仅自己部门；all 全部部门对比 ---
    view_mode = request.args.get('view', 'self' if user_dept else 'all')
    if not user_dept:
        view_mode = 'all'  # 管理员/领导班子只看全部

    # --- 4. 当前部门专属任务（用于"我的部门" Tab） ---
    my_dept_tasks = task_list
    if user_dept:
        my_dept_tasks = [t for t in task_list if t['dept_name'] == user_dept]

    my_dept_total = len(my_dept_tasks)
    my_dept_completed = sum(1 for t in my_dept_tasks if t['status'] == 'completed')
    my_dept_partial = sum(1 for t in my_dept_tasks if t['status'] == 'partial')
    my_dept_pending = sum(1 for t in my_dept_tasks if t['status'] == 'pending')
    my_dept_overdue = sum(1 for t in my_dept_tasks if is_overdue(t))
    my_dept_rate = round(my_dept_completed / my_dept_total * 100, 1) if my_dept_total > 0 else 0.0

    my_dept_upstream = [t for t in my_dept_tasks if t['st_code'] == 'upstream']
    my_dept_downstream = [t for t in my_dept_tasks if t['st_code'] == 'downstream']

    return render_template('dashboard.html',
                           month=month, tasks=task_list,
                           total=total, completed=completed,
                           pending=pending, partial=partial, overdue=overdue,
                           upstream_tasks=upstream_tasks,
                           downstream_tasks=downstream_tasks,
                           dept_ranking=dept_ranking,
                           view_mode=view_mode,
                           user_role=user_role,
                           user_dept=user_dept,
                           my_dept_tasks=my_dept_tasks,
                           my_dept_total=my_dept_total,
                           my_dept_completed=my_dept_completed,
                           my_dept_partial=my_dept_partial,
                           my_dept_pending=my_dept_pending,
                           my_dept_overdue=my_dept_overdue,
                           my_dept_rate=my_dept_rate,
                           my_dept_upstream=my_dept_upstream,
                           my_dept_downstream=my_dept_downstream,
                           server_now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


@app.route('/submit', methods=['GET'])
@require_auth
def submit():
    """提交数据列表页 - 显示所有任务及其条目完成进度"""
    db = get_db()
    month = request.args.get('month', get_current_month())
    ensure_tasks_exist(month)
    user = g.current_user
    user_dept = user.get('department', '') if user['role'] in ('director', 'liaison') else None

    sql = '''
        SELECT t.*, d.name as dept_name, d.contact_person, d.sort_order,
               st.code as st_code, st.name as st_name,
               tc.id as task_config_id, tc.deadline_day, tc.remarks as config_remarks
        FROM tasks t
        JOIN departments d ON t.department_id = d.id
        JOIN settlement_types st ON t.settlement_type_id = st.id
        JOIN task_configs tc ON t.task_config_id = tc.id
        WHERE t.month = ?
    '''
    params = [month]
    if user_dept:
        sql += ' AND d.name = ?'
        params.append(user_dept)
    sql += ' ORDER BY st.id, d.sort_order'
    tasks = db.execute(sql, params).fetchall()

    config_ids = [t['task_config_id'] for t in tasks]
    items_by_config = get_items_with_status_bulk(config_ids, month)
    task_list = []
    for t in tasks:
        items = items_by_config.get(t['task_config_id'], [])
        total_items = len(items)
        completed_items = sum(1 for i in items if i['sub_id'])
        task_dict = dict(t)
        task_dict['items'] = items
        task_dict['total_items'] = total_items
        task_dict['completed_items'] = completed_items
        # 状态已从 get_items_with_status 的结果内联得出，避免再发一次 COUNT 查询
        if total_items and completed_items >= total_items:
            task_dict['status'] = 'completed'
        elif completed_items > 0:
            task_dict['status'] = 'partial'
        else:
            task_dict['status'] = 'pending'
        task_list.append(task_dict)

    # 过滤掉没有任何条目（部门尚未配置资料）的任务，避免空任务占据列表
    task_list = [t for t in task_list if t['total_items'] > 0]

    return render_template('submit_list.html', tasks=task_list, month=month,
                           user_role=user['role'], user_dept=user_dept)


@app.route('/submit/task/<int:task_id>')
@require_auth
def submit_task(task_id):
    """提交任务详情页 - 显示该任务下所有条目，可逐条独立提交"""
    db = get_db()
    user = g.current_user
    month = request.args.get('month', get_current_month())

    task = db.execute('''
        SELECT t.*, d.name as dept_name, d.contact_person, d.sort_order,
               st.code as st_code, st.name as st_name,
               tc.id as task_config_id, tc.deadline_day, tc.remarks as config_remarks
        FROM tasks t
        JOIN departments d ON t.department_id = d.id
        JOIN settlement_types st ON t.settlement_type_id = st.id
        JOIN task_configs tc ON t.task_config_id = tc.id
        WHERE t.id = ?
    ''', (task_id,)).fetchone()
    if not task:
        abort(404)
    # 只刷新本任务配置的状态（详情页只展示单个任务，避免全量重算）
    ensure_tasks_for_month(month, only_config_id=task['task_config_id'])

    # 部门隔离：director/liaison 只能访问本部门任务
    if user['role'] in ('director', 'liaison'):
        if task['dept_name'] != user.get('department', ''):
            flash('权限不足：只能访问本部门的任务', 'danger')
            return redirect(url_for('submit'))

    items = get_items_with_status(task['task_config_id'], month)

    # 为多附件模式注入 files 列表
    sub_ids = [i['sub_id'] for i in items if i['sub_id']]
    files_map = {}
    if sub_ids:
        placeholders = ','.join('?' * len(sub_ids))
        file_rows = db.execute(
            f'SELECT id, item_submission_id, file_name FROM item_submission_files WHERE item_submission_id IN ({placeholders}) ORDER BY sort_order, id',
            sub_ids
        ).fetchall()
        for r in file_rows:
            files_map.setdefault(r['item_submission_id'], []).append({'id': r['id'], 'file_name': r['file_name']})
    items = [dict(i) for i in items]
    for i in items:
        i['files'] = files_map.get(i['sub_id'], [])

    # 获取所有模板供关联选择
    all_templates = db.execute('SELECT * FROM template_files ORDER BY name').fetchall()

    return render_template(
        'submit_task.html',
        task=task, items=items, month=month, all_templates=all_templates,
        user_role=user['role'],
        user_department=user.get('department', ''),
    )


@app.route('/submit/item/<int:item_id>', methods=['POST'])
@require_auth
def submit_item(item_id):
    """条目级提交 - 上传文件或标记为无"""
    # === 临时诊断探针（修复后删除）===
    import traceback as _tb
    try:
        return _submit_item_impl(item_id)
    except Exception as _e:
        import json as _json
        app.logger.error(f'[submit_item/{item_id}] {_e}\n{_tb.format_exc()}')
        return _json.jsonify({'error': str(_e), 'type': type(_e).__name__,
                              'tb': _tb.format_exc()[:1500]}), 500


def _submit_item_impl(item_id):
    """条目级提交实际实现（提取出来便于 try/except 包裹）"""
    db = get_db()
    month = request.form.get('month', get_current_month())
    submission_type = request.form.get('submission_type')
    submitter = request.form.get('submitter', '').strip()
    remarks = request.form.get('remarks', '').strip()

    item = db.execute('''
        SELECT ti.*, tc.department_id, tc.settlement_type_id,
               d.name as dept_name, st.name as st_name
        FROM task_items ti
        JOIN task_configs tc ON ti.task_config_id = tc.id
        JOIN departments d ON tc.department_id = d.id
        JOIN settlement_types st ON tc.settlement_type_id = st.id
        WHERE ti.id = ?
    ''', (item_id,)).fetchone()
    if not item:
        if request_is_ajax():
            return jsonify({'error': '条目不存在'}), 404
        return redirect(url_for('submit'))

    # 部门隔离 + 角色校验：director/leader 不能写；liaison 仅本部门
    if not user_can_write_to_dept(g.current_user, item['dept_name']):
        if request_is_ajax():
            return jsonify({'error': '权限不足：您不能向该部门提交/修改条目'}), 403
        flash('权限不足：您不能向该部门提交/修改条目', 'danger')
        return redirect(url_for('submit'))

    if submission_type == 'none':
        # 标记为"无" - 先删除旧提交再插入
        db.execute('DELETE FROM item_submissions WHERE task_item_id = ? AND month = ?', (item_id, month))
        db.execute('''
            INSERT INTO item_submissions (task_item_id, month, submission_type, submitter, remarks)
            VALUES (?, ?, 'none', ?, ?)
        ''', (item_id, month, submitter, remarks or '本月无对应需要收集的内容'))
        db.commit()
        flash(f'「{item["item_name"]}」已标记为"无"', 'success')

    elif submission_type == 'file':
        # 支持多附件上传（新前端使用 files[]），兼容旧客户端单文件字段 file
        files = request.files.getlist('files')
        single_file = request.files.get('file')
        if single_file and single_file.filename:
            files.insert(0, single_file)
        files = [f for f in files if f and f.filename]

        if not files:
            if request_is_ajax():
                return jsonify({'error': '请选择要上传的文件'}), 400
            flash('请选择要上传的文件', 'warning')
            return redirect(request.referrer or url_for('submit'))

        for f in files:
            if not allowed_file(f.filename):
                if request_is_ajax():
                    return jsonify({'error': '不支持的文件类型：' + f.filename}), 400
                flash(f'不支持的文件类型', 'danger')
                return redirect(request.referrer or url_for('submit'))

        # 先删除旧提交再插入（级联删除旧附件）
        db.execute('DELETE FROM item_submissions WHERE task_item_id = ? AND month = ?', (item_id, month))
        # 每个文件仅保存一次；主记录保留首个文件名（兼容旧逻辑），子表存全部
        saved = [save_upload_file(f) for f in files]
        primary_stored, primary_size, primary_name, primary_data = saved[0]
        if USE_POSTGRES:
            db.execute('''
                INSERT INTO item_submissions (task_item_id, month, submission_type, file_name, stored_name, file_data, file_size, submitter, remarks)
                VALUES (?, ?, 'file', ?, ?, ?, ?, ?, ?)
            ''', (item_id, month, primary_name, primary_stored, primary_data, primary_size, submitter, remarks))
        else:
            db.execute('''
                INSERT INTO item_submissions (task_item_id, month, submission_type, file_name, stored_name, file_size, submitter, remarks)
                VALUES (?, ?, 'file', ?, ?, ?, ?, ?)
            ''', (item_id, month, primary_name, primary_stored, primary_size, submitter, remarks))

        sub_row = db.execute('SELECT id FROM item_submissions WHERE task_item_id=? AND month=?', (item_id, month)).fetchone()
        sub_id = sub_row['id'] if sub_row else None
        # 批量插入所有附件（一次 executemany 替代逐条 INSERT，减少 Neon 往返）
        file_rows = [
            (sub_id, original, stored, data, size, blob_url, blob_pathname, i)
            for i, (stored, size, original, data, blob_url, blob_pathname) in enumerate(saved)
        ]
        if file_rows:
            db.executemany('''
                INSERT INTO item_submission_files (item_submission_id, file_name, stored_name, file_data, file_size, blob_url, blob_pathname, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', file_rows)

        db.commit()
        flash(f'「{item["item_name"]}」上传成功 {len(files)} 个文件', 'success')

    # 更新任务状态（只重算该条目所属的任务配置，避免全量重算）
    ensure_tasks_for_month(month, only_config_id=item['task_config_id'])

    if request_is_ajax():
        # AJAX 提交：返回更新后的条目信息，前端原地刷新行，避免整页重载
        sub = db.execute(
            'SELECT * FROM item_submissions WHERE task_item_id = ? AND month = ?',
            (item_id, month)
        ).fetchone()
        sub_id = sub['id'] if sub else None
        submitted_at = sub['submitted_at'] if sub else None
        if submitted_at is not None and hasattr(submitted_at, 'strftime'):
            submitted_at = submitted_at.strftime('%Y-%m-%d %H:%M')
        elif isinstance(submitted_at, str):
            submitted_at = submitted_at[:16]
        else:
            submitted_at = ''

        # 读取多附件列表
        files_list = []
        if sub_id:
            file_rows = db.execute(
                'SELECT id, file_name FROM item_submission_files WHERE item_submission_id = ? ORDER BY sort_order, id',
                (sub_id,)
            ).fetchall()
            if file_rows:
                files_list = [{'id': r['id'], 'file_name': r['file_name'],
                               'download_url': url_for('download_item_submission_file', file_id=r['id'])} for r in file_rows]
            elif sub['file_name']:
                # 兼容旧数据：没有子表记录时fallback到主表
                files_list = [{'id': None, 'file_name': sub['file_name'],
                               'download_url': url_for('download_item_submission', sub_id=sub_id)}]

        return jsonify({
            'ok': True,
            'item_id': item_id,
            'month': month,
            'submission_type': submission_type,
            'sub_id': sub_id,
            'files': files_list,
            'file_name': sub['file_name'] if sub else None,
            'submitter': submitter or (sub['submitter'] if sub else ''),
            'submitted_at': submitted_at,
        })
    return redirect(request.referrer or url_for('submit_task', task_id=request.form.get('task_id'), month=month))


@app.route('/submit/all-none/<int:task_id>', methods=['POST'])
@require_role('admin', 'liaison')
def mark_all_none(task_id):
    """将某任务下所有未提交条目标记为无"""
    db = get_db()
    month = request.form.get('month', get_current_month())
    submitter = request.form.get('submitter', '管理员').strip()

    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task:
        abort(404)

    # 部门隔离
    dept = db.execute('SELECT d.name FROM tasks t JOIN departments d ON t.department_id = d.id WHERE t.id = ?',
                      (task_id,)).fetchone()
    if dept and not user_can_write_to_dept(g.current_user, dept['name']):
        flash('权限不足：不能操作其他部门的任务', 'danger')
        return redirect(url_for('submit'))

    items = db.execute('SELECT * FROM task_items WHERE task_config_id = ? AND is_active = 1', (task['task_config_id'],)).fetchall()
    count = 0
    for item in items:
        # 只标记未提交的
        existing = db.execute('SELECT id FROM item_submissions WHERE task_item_id = ? AND month = ?', (item['id'], month)).fetchone()
        if not existing:
            db.execute('''
                INSERT INTO item_submissions (task_item_id, month, submission_type, submitter, remarks)
                VALUES (?, ?, 'none', ?, '批量标记为无')
            ''', (item['id'], month, submitter))
            count += 1
    db.commit()
    ensure_tasks_for_month(month, only_config_id=task['task_config_id'])
    if request_is_ajax():
        return jsonify({'ok': True, 'count': count})
    flash(f'已将 {count} 个条目标记为"无"', 'success')
    return redirect(url_for('submit_task', task_id=task_id, month=month))


@app.route('/summary')
@require_auth
def summary():
    """汇总视图 - 所有条目提交情况，含模板下载列"""
    month = request.args.get('month', get_current_month())
    st_code = request.args.get('type', '')
    ensure_tasks_exist(month)

    db = get_db()
    user = g.current_user
    user_dept = user.get('department', '') if user['role'] in ('director', 'liaison') else None

    query = '''
        SELECT t.*, d.name as dept_name, d.contact_person, d.sort_order,
               st.code as st_code, st.name as st_name,
               tc.id as task_config_id, tc.deadline_day, tc.remarks as config_remarks
        FROM tasks t
        JOIN departments d ON t.department_id = d.id
        JOIN settlement_types st ON t.settlement_type_id = st.id
        JOIN task_configs tc ON t.task_config_id = tc.id
        WHERE t.month = ?
    '''
    params = [month]
    if st_code in ('upstream', 'downstream'):
        query += ' AND st.code = ?'
        params.append(st_code)
    if user_dept:
        query += ' AND d.name = ?'
        params.append(user_dept)
    query += ' ORDER BY st.id, d.sort_order'

    tasks = db.execute(query, params).fetchall()

    # 收集所有条目（批量查询，避免 N+1）
    config_ids = [t['task_config_id'] for t in tasks]
    items_by_config = get_items_with_status_bulk(config_ids, month)
    all_items = []
    for t in tasks:
        items = items_by_config.get(t['task_config_id'], [])
        for item in items:
            item_dict = dict(item)
            item_dict['dept_name'] = t['dept_name']
            item_dict['st_code'] = t['st_code']
            item_dict['st_name'] = t['st_name']
            item_dict['deadline_day'] = t['deadline_day']
            item_dict['config_remarks'] = t['config_remarks']
            item_dict['task_id'] = t['id']
            all_items.append(item_dict)

    total = len(all_items)
    completed = sum(1 for i in all_items if i['sub_id'])
    pending = total - completed

    # 按部门汇总
    dept_summary = {}
    for item in all_items:
        dept = item['dept_name']
        if dept not in dept_summary:
            dept_summary[dept] = {'total': 0, 'completed': 0, 'pending': 0}
        dept_summary[dept]['total'] += 1
        if item['sub_id']:
            dept_summary[dept]['completed'] += 1
        else:
            dept_summary[dept]['pending'] += 1

    return render_template('summary.html',
                           month=month, items=all_items,
                           total=total, completed=completed,
                           pending=pending,
                           dept_summary=dept_summary,
                           st_code=st_code)


# ===========================================================================
# API - 条目编辑/增删/模板关联
# ===========================================================================
@app.route('/api/item/edit', methods=['POST'])
@require_role('admin', 'liaison')
def api_edit_item():
    """编辑条目名称（管理员 / 联络人）"""
    db = get_db()
    item_id = request.form.get('item_id')
    item_name = request.form.get('item_name', '').strip()
    description = request.form.get('description', '').strip()

    if not item_name:
        return jsonify({'error': '条目名称不能为空'}), 400

    item = db.execute('SELECT * FROM task_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '条目不存在'}), 404
    # 部门隔离
    _, dept = _check_task_dept_or_403(item['task_config_id'])
    if not user_can_write_to_dept(g.current_user, dept or ''):
        return jsonify({'error': '只能编辑本部门条目'}), 403

    db.execute('UPDATE task_items SET item_name = ?, description = ? WHERE id = ?',
               (item_name, description, item_id))
    db.commit()
    return jsonify({'ok': True, 'item_name': item_name, 'description': description})


@app.route('/api/item/add', methods=['POST'])
@require_role('admin', 'liaison')
def api_add_item():
    """新增条目（管理员 / 联络人）"""
    db = get_db()
    task_config_id = request.form.get('task_config_id')
    item_name = request.form.get('item_name', '').strip()
    template_file_id = request.form.get('template_file_id') or None

    if not item_name:
        return jsonify({'error': '条目名称不能为空'}), 400

    _, dept = _check_task_dept_or_403(task_config_id)
    if not user_can_write_to_dept(g.current_user, dept or ''):
        return jsonify({'error': '只能在本部门任务下新增条目'}), 403

    max_order = db.execute('SELECT MAX(sort_order) as m FROM task_items WHERE task_config_id = ?', (task_config_id,)).fetchone()['m'] or 0
    db.execute('''
        INSERT INTO task_items (task_config_id, item_name, template_file_id, sort_order, is_active)
        VALUES (?,?,?,?,1)
    ''', (task_config_id, item_name, template_file_id, max_order + 1))
    db.commit()
    new_id = db.execute('SELECT MAX(id) as m FROM task_items WHERE task_config_id=?', (task_config_id,)).fetchone()['m']

    tpl_name = None
    if template_file_id:
        tpl = db.execute('SELECT name FROM template_files WHERE id = ?', (template_file_id,)).fetchone()
        tpl_name = tpl['name'] if tpl else None
    return jsonify({'ok': True, 'id': new_id, 'item_name': item_name, 'template_file_id': template_file_id, 'template_name': tpl_name})


@app.route('/api/item/delete', methods=['POST'])
@require_role('admin', 'liaison')
def api_delete_item():
    """删除条目（管理员 / 联络人）"""
    db = get_db()
    item_id = request.form.get('item_id')
    item = db.execute('SELECT * FROM task_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '条目不存在'}), 404
    _, dept = _check_task_dept_or_403(item['task_config_id'])
    if not user_can_write_to_dept(g.current_user, dept or ''):
        return jsonify({'error': '只能删除本部门条目'}), 403

    db.execute('DELETE FROM item_submissions WHERE task_item_id = ?', (item_id,))
    db.execute('DELETE FROM task_items WHERE id = ?', (item_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/item/link-template', methods=['POST'])
@require_role('admin', 'liaison')
def api_link_template():
    """关联条目与模板文件（管理员 / 联络人）"""
    db = get_db()
    item_id = request.form.get('item_id')
    template_file_id = request.form.get('template_file_id') or None

    item = db.execute('SELECT * FROM task_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '条目不存在'}), 404
    _, dept = _check_task_dept_or_403(item['task_config_id'])
    if not user_can_write_to_dept(g.current_user, dept or ''):
        return jsonify({'error': '只能操作本部门条目'}), 403

    if template_file_id:
        tpl = db.execute('SELECT * FROM template_files WHERE id = ?', (template_file_id,)).fetchone()
        if not tpl:
            return jsonify({'error': '模板文件不存在'}), 404
        db.execute('UPDATE task_items SET template_file_id = ? WHERE id = ?', (template_file_id, item_id))
        db.commit()
        return jsonify({'ok': True, 'tpl_id': tpl['id'], 'tpl_name': tpl['name']})
    else:
        db.execute('UPDATE task_items SET template_file_id = NULL WHERE id = ?', (item_id,))
        db.commit()
        return jsonify({'ok': True, 'tpl_id': None, 'tpl_name': None})


@app.route('/api/item/unsubmit', methods=['POST'])
@require_role('admin', 'liaison')
def api_unsubmit_item():
    """撤销条目提交（管理员 / 联络人）"""
    db = get_db()
    item_id = request.form.get('item_id')
    month = request.form.get('month', get_current_month())

    sub = db.execute('SELECT * FROM item_submissions WHERE task_item_id = ? AND month = ?', (item_id, month)).fetchone()
    if not sub:
        return jsonify({'error': '无提交记录'}), 404

    item = db.execute('SELECT * FROM task_items WHERE id = ?', (item_id,)).fetchone()
    if item:
        _, dept = _check_task_dept_or_403(item['task_config_id'])
        if not user_can_write_to_dept(g.current_user, dept or ''):
            return jsonify({'error': '只能撤销本部门的提交'}), 403

    # 如果有文件，删除物理文件（本地模式）
    if not USE_POSTGRES and sub['stored_name']:
        filepath = os.path.join(UPLOAD_DIR, 'item_submissions', sub['stored_name'])
        if os.path.exists(filepath):
            os.remove(filepath)

    db.execute('DELETE FROM item_submissions WHERE id = ?', (sub['id'],))
    db.commit()
    # 只重算被删条目所属任务配置的状态，避免对全部 14 个配置重算
    if item:
        ensure_tasks_for_month(month, only_config_id=item['task_config_id'])
    else:
        ensure_tasks_for_month(month)
    return jsonify({'ok': True})


@app.route('/api/item/edit-remarks', methods=['POST'])
@require_role('admin')
def api_edit_item_remarks():
    """修改部门提交条目备注（仅合同管理部可操作任意部门）

    不影响已上传的文件/无标记，仅更新 item_submissions.remarks。
    适用于"部门联络人提交时漏写或写错备注，事后由合同管理部统一修正"的场景。
    """
    db = get_db()
    item_id = request.form.get('item_id')
    month = request.form.get('month', get_current_month())
    remarks = request.form.get('remarks', '').strip()

    if not item_id:
        return jsonify({'error': '缺少条目 ID'}), 400
    if remarks and len(remarks) > 500:
        return jsonify({'error': '备注不能超过 500 个字符'}), 400

    # 仅合同管理部可执行（虽然 admin 角色都能访问本接口，但额外卡一道部门判断）
    if g.current_user.get('department', '') != '合同管理部':
        return jsonify({'error': '仅合同管理部可修改部门提交备注'}), 403

    # 必须先有提交记录才能修改备注（不允许凭空加备注）
    sub = db.execute('SELECT * FROM item_submissions WHERE task_item_id = ? AND month = ?', (item_id, month)).fetchone()
    if not sub:
        return jsonify({'error': '该条目暂无提交记录，无法修改备注'}), 404

    # 任意部门都可改（合同管理部 admin 全部门通行）
    item = db.execute('SELECT * FROM task_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '条目不存在'}), 404

    db.execute('UPDATE item_submissions SET remarks = ? WHERE id = ?', (remarks, sub['id']))
    db.commit()
    return jsonify({'ok': True, 'remarks': remarks})


# ===========================================================================
# 部门联络人 / 截止日期更新（管理员登录后直接可用）
# ===========================================================================
@app.route('/api/config/update-deadline', methods=['POST'])
@require_role('admin')
def api_update_deadline():
    """更新任务的截止日期（task_configs.deadline_day）"""
    db = get_db()
    task_config_id = request.form.get('task_config_id')
    deadline_day = request.form.get('deadline_day')

    # 校验
    try:
        task_config_id = int(task_config_id)
        deadline_day = int(deadline_day)
    except (TypeError, ValueError):
        return jsonify({'error': '参数类型错误'}), 400

    if deadline_day < 1 or deadline_day > 31:
        return jsonify({'error': '截止日期必须在 1-31 之间'}), 400

    cfg = db.execute('SELECT * FROM task_configs WHERE id = ?', (task_config_id,)).fetchone()
    if not cfg:
        return jsonify({'error': '配置项不存在'}), 404

    db.execute('UPDATE task_configs SET deadline_day = ? WHERE id = ?', (deadline_day, task_config_id))
    db.commit()
    return jsonify({'ok': True, 'task_config_id': task_config_id, 'deadline_day': deadline_day})


@app.route('/api/config/update-contact', methods=['POST'])
@require_role('admin')
def api_update_contact():
    """更新部门联系人姓名"""
    db = get_db()
    department_id = request.form.get('department_id')
    contact_person = (request.form.get('contact_person') or '').strip()
    try:
        department_id = int(department_id)
    except (TypeError, ValueError):
        return jsonify({'error': '部门参数无效'}), 400
    if len(contact_person) > 50:
        return jsonify({'error': '联系人姓名不能超过50个字符'}), 400
    department = db.execute('SELECT id FROM departments WHERE id = ?', (department_id,)).fetchone()
    if not department:
        return jsonify({'error': '部门不存在'}), 404
    db.execute('UPDATE departments SET contact_person = ? WHERE id = ?', (contact_person or None, department_id))
    db.commit()
    return jsonify({'ok': True, 'department_id': department_id, 'contact_person': contact_person})


@app.route('/api/config/add-department', methods=['POST'])
@require_role('admin')
def api_add_department():
    """新增部门（admin 专用）

    - 接收部门名 + 联络人（可选）+ 默认截止日（1-31）
    - 事务内：INSERT INTO departments；然后查 up/down 两条 settlement_type_id
      各建一条 task_configs（required_materials='（请编辑需提供资料）'，
      deadline_day 由表单传入，remarks='', is_active=1）；调用 split_materials
      初始化 task_items（与 Excel 导入行为一致）。
    - sort_order 取当前最大 + 5（留中间空位，方便后续插入新部门）。
    - 返回新部门 id 和对应对上/对下 task_config_id（前端按需刷新页面）。
    """
    db = get_db()
    name = (request.form.get('name') or '').strip()
    contact_person = (request.form.get('contact_person') or '').strip()
    deadline_raw = (request.form.get('deadline_day') or '28').strip()

    if not name:
        return jsonify({'error': '部门名不能为空'}), 400
    if len(name) > 50:
        return jsonify({'error': '部门名不能超过 50 个字符'}), 400
    if contact_person and len(contact_person) > 50:
        return jsonify({'error': '联络人姓名不能超过 50 个字符'}), 400
    try:
        deadline_day = int(deadline_raw)
    except (TypeError, ValueError):
        return jsonify({'error': '截止日必须为 1-31 之间的整数'}), 400
    if deadline_day < 1 or deadline_day > 31:
        return jsonify({'error': '截止日必须在 1-31 之间'}), 400

    existing = db.execute('SELECT id FROM departments WHERE name = ?', (name,)).fetchone()
    if existing:
        return jsonify({'error': f'已存在同名部门「{name}」'}), 409

    max_order = db.execute('SELECT MAX(sort_order) FROM departments').fetchone()[0] or 0
    new_sort = (max_order or 0) + 5

    db.execute(
        'INSERT INTO departments (name, contact_person, sort_order) VALUES (?, ?, ?)',
        (name, contact_person or None, new_sort)
    )
    if USE_POSTGRES:
        new_dept_id = db.execute('SELECT lastval()').fetchone()[0]
    else:
        new_dept_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    # 自动建对上 / 对下 两条 task_configs
    settlement_types = db.execute('SELECT id, code FROM settlement_types').fetchall()
    created = []
    placeholder_materials = '（请编辑需提供资料）'
    for st in settlement_types:
        # 防御：万一历史脏数据已有同 (dept, st)，跳过避免 UNIQUE 冲突
        dup = db.execute(
            'SELECT id FROM task_configs WHERE department_id = ? AND settlement_type_id = ?',
            (new_dept_id, st['id'])
        ).fetchone()
        if dup:
            created.append({'settlement_type': st['code'], 'task_config_id': dup['id'], 'existed': True})
            continue
        db.execute('''
            INSERT INTO task_configs
                (department_id, settlement_type_id, required_materials, deadline_day, remarks, is_active)
            VALUES (?, ?, ?, ?, '', 1)
        ''', (new_dept_id, st['id'], placeholder_materials, deadline_day))
        if USE_POSTGRES:
            new_cfg_id = db.execute('SELECT lastval()').fetchone()[0]
        else:
            new_cfg_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        # 拆分条目（与 Excel 导入行为一致）；老库若 items_initialized 列不存在则忽略 UPDATE
        items = split_materials(placeholder_materials)
        if not items:
            items = ['请编辑需提供资料']
        for idx, item_name in enumerate(items):
            db.execute('''
                INSERT INTO task_items (task_config_id, item_name, sort_order, is_active)
                VALUES (?, ?, ?, 1)
            ''', (new_cfg_id, item_name, idx))
        try:
            db.execute('UPDATE task_configs SET items_initialized = 1 WHERE id = ?', (new_cfg_id,))
        except Exception:
            pass  # 老库无该列，跳过（条目已建好，效果一致）
        created.append({'settlement_type': st['code'], 'task_config_id': new_cfg_id, 'existed': False})

    db.commit()
    return jsonify({
        'ok': True,
        'department_id': new_dept_id,
        'department_name': name,
        'created_configs': created,
    })


@app.route('/api/config/delete-department', methods=['POST'])
@require_role('admin')
def api_delete_department():
    """删除部门（admin 专用）

    安全策略：
    - 必须 confirm_delete = 'on'（前端必须勾选二次确认）
    - 该部门任何 cfg 含 submissions（提交历史）→ 400 拒绝，防止误删数据
    - 否则事务内删除 task_items → task_configs → departments
    """
    db = get_db()
    department_id = request.form.get('department_id')
    confirm = request.form.get('confirm_delete', '')

    if confirm != 'on':
        return jsonify({'error': '请勾选确认删除'}), 400

    try:
        department_id = int(department_id)
    except (TypeError, ValueError):
        return jsonify({'error': '部门参数无效'}), 400

    department = db.execute(
        'SELECT id, name FROM departments WHERE id = ?', (department_id,)
    ).fetchone()
    if not department:
        return jsonify({'error': '部门不存在'}), 404
    dept_name = department['name']

    # 安全闸门：有提交历史的部门禁止删除（即使勾选了也拒绝）
    sub_count = db.execute('''
        SELECT COUNT(*) AS cnt FROM submissions
        WHERE task_id IN (SELECT id FROM task_configs WHERE department_id = ?)
    ''', (department_id,)).fetchone()['cnt']
    if sub_count > 0:
        return jsonify({
            'error': f'部门「{dept_name}」有 {sub_count} 条提交记录，禁止删除（请先在 Neon Console 手动处理）',
            'submission_count': sub_count,
        }), 400

    # 统计待删除项（事务前回执用）
    cfg_ids_rows = db.execute(
        'SELECT id FROM task_configs WHERE department_id = ?', (department_id,)
    ).fetchall()
    cfg_ids = [r['id'] for r in cfg_ids_rows]
    cfg_cnt = len(cfg_ids)
    item_cnt = 0
    if cfg_ids:
        placeholders = ','.join('?' * len(cfg_ids))
        item_cnt = db.execute(
            f'SELECT COUNT(*) AS cnt FROM task_items WHERE task_config_id IN ({placeholders})',
            cfg_ids
        ).fetchone()['cnt']

    # 事务内清理
    try:
        if cfg_ids:
            placeholders = ','.join('?' * len(cfg_ids))
            # 严格子表顺序：tasks → task_items → task_configs
            # tasks.task_config_id 是外键，先解除；task_items 同理
            db.execute(
                f'DELETE FROM tasks WHERE task_config_id IN ({placeholders})',
                cfg_ids
            )
            db.execute(
                f'DELETE FROM task_items WHERE task_config_id IN ({placeholders})',
                cfg_ids
            )
            db.execute(
                f'DELETE FROM task_configs WHERE department_id = ?',
                (department_id,)
            )
        db.execute('DELETE FROM departments WHERE id = ?', (department_id,))
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({'error': f'删除失败：{e}'}), 500

    return jsonify({
        'ok': True,
        'department_id': department_id,
        'department_name': dept_name,
        'deleted_task_configs': cfg_cnt,
        'deleted_task_items': item_cnt,
    })


@app.route('/api/config/update-materials-remarks', methods=['POST'])
@require_role('admin')
def api_update_materials_remarks():
    """更新任务配置的「需提供资料」与「备注」（admin 专用）

    - 同时更新 task_configs.required_materials 与 task_configs.remarks
    - 修改 required_materials 后自动重新拆分 task_items（与 Excel 导入行为一致）
    - 任意长度文本（前端用 textarea + 500 字校验）
    """
    db = get_db()
    task_config_id = request.form.get('task_config_id')
    required_materials = (request.form.get('required_materials') or '').strip()
    remarks = (request.form.get('remarks') or '').strip()

    try:
        task_config_id = int(task_config_id)
    except (TypeError, ValueError):
        return jsonify({'error': '参数类型错误'}), 400

    if not required_materials:
        return jsonify({'error': '「需提供资料」不能为空'}), 400
    if len(required_materials) > 1000:
        return jsonify({'error': '「需提供资料」内容过长（>1000字），请精简'}), 400
    if len(remarks) > 500:
        return jsonify({'error': '「备注」不能超过 500 个字符'}), 400

    cfg = db.execute('SELECT * FROM task_configs WHERE id = ?', (task_config_id,)).fetchone()
    if not cfg:
        return jsonify({'error': '配置项不存在'}), 404

    db.execute('''
        UPDATE task_configs SET required_materials = ?, remarks = ? WHERE id = ?
    ''', (required_materials, remarks, task_config_id))

    # 仅当材料文本相对旧值变化时才重新拆分条目（避免误删提交记录）
    if required_materials != (cfg['required_materials'] or ''):
        db.execute('DELETE FROM task_items WHERE task_config_id = ?', (task_config_id,))
        items = split_materials(required_materials)
        if not items:
            items = [required_materials or '需提交资料']
        for idx, item_name in enumerate(items):
            db.execute('''
                INSERT INTO task_items (task_config_id, item_name, sort_order, is_active)
                VALUES (?, ?, ?, 1)
            ''', (task_config_id, item_name, idx))
        auto_link_templates(db)

    db.commit()
    return jsonify({
        'ok': True,
        'task_config_id': task_config_id,
        'required_materials': required_materials,
        'remarks': remarks,
    })


# ===========================================================================
# 文件下载路由
# ===========================================================================
@app.route('/download/item-submission/<int:sub_id>')
def download_item_submission(sub_id):
    """下载条目提交的文件"""
    db = get_db()
    sub = db.execute('SELECT * FROM item_submissions WHERE id = ?', (sub_id,)).fetchone()
    if not sub or not sub['stored_name']:
        abort(404)
    if USE_POSTGRES:
        if not sub['file_data']:
            abort(404)
        return send_file(io.BytesIO(sub['file_data']), as_attachment=True, download_name=sub['file_name'])
    filepath = os.path.join(UPLOAD_DIR, 'item_submissions', sub['stored_name'])
    if not os.path.exists(filepath):
        flash('文件不存在', 'danger')
        return redirect(request.referrer or url_for('summary'))
    return send_file(filepath, as_attachment=True, download_name=sub['file_name'])


@app.route('/download/item-submission-file/<int:file_id>')
def download_item_submission_file(file_id):
    """下载条目提交的单个附件（多附件模式下使用）"""
    db = get_db()
    row = db.execute('SELECT * FROM item_submission_files WHERE id = ?', (file_id,)).fetchone()
    if not row:
        abort(404)
    # 启用 Blob 时直接重定向到对象存储 URL（鉴权已在路由层完成）
    if row['blob_pathname'] and row['blob_url']:
        return redirect(row['blob_url'])
    if USE_POSTGRES:
        if not row['file_data']:
            abort(404)
        return send_file(io.BytesIO(row['file_data']), as_attachment=True, download_name=row['file_name'])
    filepath = os.path.join(UPLOAD_DIR, 'item_submissions', row['stored_name'])
    if not os.path.exists(filepath):
        flash('文件不存在', 'danger')
        return redirect(request.referrer or url_for('summary'))
    return send_file(filepath, as_attachment=True, download_name=row['file_name'])


@app.route('/api/item-submission-file/<int:file_id>', methods=['DELETE'])
@require_auth
def api_delete_item_submission_file(file_id):
    """删除条目提交的某个附件（多附件模式）；仅该部门可写角色（admin / 本部门 liaison）可操作。"""
    db = get_db()
    user = g.current_user
    row = db.execute('''
        SELECT sf.*, d.name as dept_name
        FROM item_submission_files sf
        JOIN item_submissions isub ON sf.item_submission_id = isub.id
        JOIN task_items ti ON isub.task_item_id = ti.id
        JOIN task_configs tc ON ti.task_config_id = tc.id
        JOIN departments d ON tc.department_id = d.id
        WHERE sf.id = ?
    ''', (file_id,)).fetchone()
    if not row:
        return jsonify({'error': '附件不存在'}), 404
    if not user_can_write_to_dept(user, row['dept_name']):
        return jsonify({'error': '权限不足：您不能修改该部门的数据'}), 403
    if not USE_POSTGRES:
        filepath = os.path.join(UPLOAD_DIR, 'item_submissions', row['stored_name'])
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
    db.execute('DELETE FROM item_submission_files WHERE id = ?', (file_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/download/template/<int:tid>')
def download_template_by_id(tid):
    """下载模板文件"""
    db = get_db()
    tpl = db.execute('SELECT * FROM template_files WHERE id = ?', (tid,)).fetchone()
    if not tpl:
        abort(404)
    # 启用 Blob 时直接重定向到对象存储 URL（鉴权已在路由层完成）
    if tpl['blob_pathname'] and tpl['blob_url']:
        return redirect(tpl['blob_url'])
    if USE_POSTGRES:
        if not tpl['file_data']:
            abort(404)
        return send_file(io.BytesIO(tpl['file_data']), as_attachment=True, download_name=tpl['file_name'])
    filepath = os.path.join(TEMPLATE_DIR, tpl['stored_name'])
    if not os.path.exists(filepath):
        flash('文件不存在，可能已被删除', 'danger')
        return redirect(request.referrer or url_for('templates_page'))
    return send_file(filepath, as_attachment=True, download_name=tpl['file_name'])


# ===========================================================================
# 模板文件管理
# ===========================================================================
@app.route('/templates')
@require_role('admin', 'liaison')
def templates_page():
    db = get_db()
    search = request.args.get('q', '').strip()
    st_filter = request.args.get('type', '')

    query = 'SELECT * FROM template_files'
    params = []
    conditions = []
    if search:
        conditions.append('(name LIKE ? OR description LIKE ? OR department LIKE ?)')
        params.extend([f'%{search}%'] * 3)
    if st_filter:
        conditions.append('settlement_type = ?')
        params.append(st_filter)
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY uploaded_at DESC'

    templates = db.execute(query, params).fetchall()
    return render_template('templates.html', templates=templates, search=search, st_filter=st_filter)


@app.route('/templates/upload', methods=['POST'])
@require_role('admin', 'liaison')
def upload_template():
    db = get_db()
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    settlement_type = request.form.get('settlement_type', 'common').strip()
    department = request.form.get('department', '').strip()
    uploader = request.form.get('uploader', '').strip()

    files = request.files.getlist('files')
    single_file = request.files.get('file')
    if single_file and single_file.filename:
        files.insert(0, single_file)
    files = [f for f in files if f and f.filename]

    if not files:
        if request_is_ajax():
            return jsonify({'error': '请选择文件'}), 400
        flash('请选择文件', 'warning')
        return redirect(url_for('templates_page'))

    bad = [f.filename for f in files if not allowed_file(f.filename)]
    if bad:
        if request_is_ajax():
            return jsonify({'error': '不支持的文件类型：' + ', '.join(bad)}), 400
        flash('不支持的文件类型', 'danger')
        return redirect(url_for('templates_page'))

    st_names = {
        'upstream': '对上结算',
        'downstream': '对下结算',
        'common': '通用'
    }
    ts_name = st_names.get(settlement_type, '通用')
    multi = len(files) > 1

    for i, file in enumerate(files):
        base_name = name if name else os.path.splitext(file.filename)[0]
        # 多文件时名称追加序号，避免重名
        tpl_name = f"{base_name} ({i+1})" if multi else base_name

        stored, file_size, original, data, blob_url, blob_pathname = save_upload_file(file, subdir='template_files')
        db.execute('''
            INSERT INTO template_files (name, description, settlement_type, department, file_name, stored_name, file_data, file_size, blob_url, blob_pathname, uploader)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ''', (tpl_name, description, ts_name, department,
              original, stored, data, file_size, blob_url, blob_pathname, uploader))

    db.commit()
    if request_is_ajax():
        return jsonify({'ok': True, 'count': len(files)})
    flash(f'模板文件上传成功 {len(files)} 个', 'success')
    return redirect(url_for('templates_page'))


@app.route('/templates/download/<int:tid>')
def download_template(tid):
    """下载模板文件（兼容旧路由）"""
    return download_template_by_id(tid)


@app.route('/templates/delete/<int:tid>', methods=['POST'])
@require_role('admin', 'liaison')
def delete_template(tid):
    db = get_db()
    tpl = db.execute('SELECT * FROM template_files WHERE id = ?', (tid,)).fetchone()
    if tpl:
        if not USE_POSTGRES:
            filepath = os.path.join(TEMPLATE_DIR, tpl['stored_name'])
            if os.path.exists(filepath):
                os.remove(filepath)
        # 解除条目关联
        db.execute('UPDATE task_items SET template_file_id = NULL WHERE template_file_id = ?', (tid,))
        db.execute('DELETE FROM template_files WHERE id = ?', (tid,))
        db.commit()
        flash(f'模板文件「{tpl["name"]}」已删除', 'info')
    return redirect(url_for('templates_page'))


@app.route('/download/<int:sub_id>')
def download_submission(sub_id):
    """下载提交文件（兼容旧路由，使用 submissions 表）"""
    db = get_db()
    sub = db.execute('SELECT * FROM submissions WHERE id = ?', (sub_id,)).fetchone()
    if not sub or not sub['stored_name']:
        abort(404)
    filepath = os.path.join(UPLOAD_DIR, 'submissions', sub['stored_name'])
    if not os.path.exists(filepath):
        flash('文件不存在', 'danger')
        return redirect(url_for('summary'))
    return send_file(filepath, as_attachment=True, download_name=sub['file_name'])


# ===========================================================================
# 配置管理
# ===========================================================================
@app.route('/config')
@require_role('admin')
def config_page():
    db = get_db()
    departments = db.execute('SELECT * FROM departments ORDER BY sort_order').fetchall()
    configs = db.execute('''
        SELECT tc.*, d.name as dept_name, d.contact_person, d.sort_order,
               st.code as st_code, st.name as st_name,
               (SELECT COUNT(*) FROM task_items WHERE task_config_id = tc.id AND is_active = 1) as item_count
        FROM task_configs tc
        JOIN departments d ON tc.department_id = d.id
        JOIN settlement_types st ON tc.settlement_type_id = st.id
        ORDER BY st.id, d.sort_order
    ''').fetchall()
    config_history = db.execute('SELECT * FROM config_files ORDER BY uploaded_at DESC LIMIT 10').fetchall()

    return render_template('config.html',
                           departments=departments,
                           configs=configs,
                           config_history=config_history)


@app.route('/config/upload', methods=['POST'])
def upload_config():
    db = get_db()
    file = request.files.get('file')
    uploader = request.form.get('uploader', '').strip()

    if not file or not file.filename:
        flash('请选择配置文件', 'warning')
        return redirect(url_for('config_page'))

    filename = file.filename.lower()
    if not (filename.endswith('.xlsx') or filename.endswith('.xls') or filename.endswith('.csv')):
        flash('配置文件仅支持 Excel(.xlsx/.xls) 或 CSV 格式', 'danger')
        return redirect(url_for('config_page'))

    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    original = secure_filename(file.filename)
    if not original:
        original = 'config.xlsx'
    stored_name = f"{timestamp}_{original}"
    if USE_POSTGRES:
        filepath = os.path.join('/tmp', stored_name)
    else:
        config_dir = os.path.join(UPLOAD_DIR, 'configs')
        os.makedirs(config_dir, exist_ok=True)
        filepath = os.path.join(config_dir, stored_name)
    file.save(filepath)

    try:
        configs_data = parse_config_file(filepath)
    except Exception as e:
        flash(f'配置文件解析失败：{str(e)}', 'danger')
        return redirect(url_for('config_page'))

    if not configs_data:
        flash('配置文件中未找到有效的配置数据，请检查格式', 'warning')
        return redirect(url_for('config_page'))

    dept_count = 0
    task_count = 0

    for cfg in configs_data:
        dept = db.execute('SELECT id FROM departments WHERE name = ?', (cfg['department'],)).fetchone()
        if not dept:
            max_order = db.execute('SELECT MAX(sort_order) FROM departments').fetchone()[0] or 0
            db.execute('INSERT INTO departments (name, sort_order) VALUES (?, ?)',
                       (cfg['department'], max_order + 1))
            db.commit()
            dept = db.execute('SELECT id FROM departments WHERE name = ?', (cfg['department'],)).fetchone()
            dept_count += 1

        st_code = cfg['settlement_type']
        st = db.execute('SELECT id FROM settlement_types WHERE code = ?', (st_code,)).fetchone()
        if not st:
            continue

        existing = db.execute('''
            SELECT id FROM task_configs
            WHERE department_id = ? AND settlement_type_id = ?
        ''', (dept[0], st[0])).fetchone()

        if existing:
            db.execute('''
                UPDATE task_configs
                SET required_materials = ?, deadline_day = ?, remarks = ?, is_active = 1
                WHERE id = ?
            ''', (cfg['required_materials'], cfg['deadline_day'], cfg.get('remarks', ''), existing[0]))
            # 重新拆分条目
            db.execute('DELETE FROM task_items WHERE task_config_id = ?', (existing[0],))
            items = split_materials(cfg['required_materials'])
            if not items:
                items = [cfg['required_materials'] or '需提交资料']
            for idx, item_name in enumerate(items):
                db.execute('''
                    INSERT INTO task_items (task_config_id, item_name, sort_order, is_active)
                    VALUES (?,?,?,1)
                ''', (existing[0], item_name, idx))
        else:
            db.execute('''
                INSERT INTO task_configs
                (department_id, settlement_type_id, required_materials, deadline_day, remarks, is_active)
                VALUES (?,?,?,?,?,1)
            ''', (dept[0], st[0], cfg['required_materials'], cfg['deadline_day'], cfg.get('remarks', '')))
            new_config_id = db.execute('SELECT MAX(id) as m FROM task_configs').fetchone()['m']
            items = split_materials(cfg['required_materials'])
            if not items:
                items = [cfg['required_materials'] or '需提交资料']
            for idx, item_name in enumerate(items):
                db.execute('''
                    INSERT INTO task_items (task_config_id, item_name, sort_order, is_active)
                    VALUES (?,?,?,1)
                ''', (new_config_id, item_name, idx))
        task_count += 1

    db.commit()
    auto_link_templates(db)

    db.execute('''
        INSERT INTO config_files (file_name, stored_name, departments_count, tasks_count, uploader)
        VALUES (?,?,?,?,?)
    ''', (original, stored_name, dept_count, task_count, uploader))
    db.commit()

    flash(f'配置文件导入成功！新增部门 {dept_count} 个，更新任务配置 {task_count} 项', 'success')
    return redirect(url_for('config_page'))


def parse_config_file(filepath):
    configs = []
    filename = filepath.lower()

    if filename.endswith('.csv'):
        import csv
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            rows = list(reader)
    else:
        import openpyxl
        wb = openpyxl.load_workbook(filepath, data_only=True)
        rows = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                rows.append([str(v).strip() if v is not None else '' for v in row])

    if not rows:
        return configs

    header_idx = -1
    col_map = {}
    for i, row in enumerate(rows[:10]):
        row_lower = [str(c).lower().strip() for c in row]
        has_dept = any('部门' in c for c in row_lower)
        has_material = any('资料' in c or '材料' in c or '内容' in c for c in row_lower)
        if has_dept and has_material:
            header_idx = i
            for j, cell in enumerate(row_lower):
                if '结算' in cell or '类型' in cell:
                    col_map['settlement_type'] = j
                elif '部门' in cell:
                    col_map['department'] = j
                elif '资料' in cell or '材料' in cell or '内容' in cell:
                    col_map['required_materials'] = j
                elif '截止' in cell or '时间' in cell or '日期' in cell or '日前' in cell:
                    col_map['deadline_day'] = j
                elif '备注' in cell or '说明' in cell or '作用' in cell:
                    col_map['remarks'] = j
            break

    if header_idx == -1:
        for row in rows:
            if len(row) >= 4 and row[0] and row[1]:
                st_raw = row[0]
                st_code = 'upstream' if ('对上' in st_raw or 'upstream' in st_raw.lower()) else \
                          'downstream' if ('对下' in st_raw or 'downstream' in st_raw.lower()) else None
                if st_code:
                    try:
                        deadline = int(''.join(filter(str.isdigit, row[3]))) if len(row) > 3 else 30
                    except:
                        deadline = 30
                    configs.append({
                        'settlement_type': st_code,
                        'department': row[1],
                        'required_materials': row[2] if len(row) > 2 else '',
                        'deadline_day': deadline,
                        'remarks': row[4] if len(row) > 4 else ''
                    })
        return configs

    for row in rows[header_idx + 1:]:
        if not row or all(not str(c).strip() for c in row):
            continue

        dept = row[col_map['department']].strip() if 'department' in col_map else ''
        if not dept:
            continue

        st_raw = row[col_map['settlement_type']].strip() if 'settlement_type' in col_map else ''
        st_code = 'upstream' if ('对上' in st_raw or 'upstream' in st_raw.lower()) else \
                  'downstream' if ('对下' in st_raw or 'downstream' in st_raw.lower()) else None
        if not st_code:
            continue

        materials = row[col_map['required_materials']].strip() if 'required_materials' in col_map else ''
        deadline_str = row[col_map['deadline_day']].strip() if 'deadline_day' in col_map else '30'
        try:
            deadline = int(''.join(filter(str.isdigit, deadline_str)))
            if deadline < 1 or deadline > 31:
                deadline = 30
        except:
            deadline = 30

        remarks = row[col_map['remarks']].strip() if 'remarks' in col_map else ''

        configs.append({
            'settlement_type': st_code,
            'department': dept,
            'required_materials': materials,
            'deadline_day': deadline,
            'remarks': remarks
        })

    return configs


# ===========================================================================
# 使用指引
# ===========================================================================
@app.route('/guide')
@require_auth
def guide():
    db = get_db()
    fee_rates = db.execute('SELECT * FROM fee_rates ORDER BY id').fetchall()
    custom = db.execute('SELECT content FROM guide_content WHERE id=1').fetchone()
    custom_content = custom['content'] if custom else None
    return render_template('guide.html', fee_rates=fee_rates, custom_content=custom_content)


@app.route('/api/guide/save', methods=['POST'])
def api_save_guide():
    content = request.form.get('content', '')
    if not content.strip():
        return jsonify({'ok': False, 'error': '内容不能为空'}), 400
    db = get_db()
    db.execute('''
        INSERT INTO guide_content (id, content, updated_at) VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at
    ''', (content, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    db.commit()
    return jsonify({'ok': True, 'message': '保存成功'})


@app.route('/api/guide/reset', methods=['POST'])
def api_reset_guide():
    db = get_db()
    db.execute('DELETE FROM guide_content WHERE id=1')
    db.commit()
    return jsonify({'ok': True, 'message': '已恢复默认内容'})


@app.route('/api/fee-rates', methods=['GET'])
def api_get_fee_rates():
    db = get_db()
    rates = db.execute('SELECT * FROM fee_rates ORDER BY id').fetchall()
    return jsonify([dict(r) for r in rates])


@app.route('/api/fee-rates/update', methods=['POST'])
def api_update_fee_rate():
    db = get_db()
    fee_type = request.form.get('fee_type')
    rate_value = request.form.get('rate_value', '').strip()
    rate_unit = request.form.get('rate_unit', '').strip()
    calc_method = request.form.get('calc_method', '').strip()
    note = request.form.get('note', '').strip()

    if not fee_type:
        return jsonify({'error': '缺少费用类型'}), 400

    existing = db.execute('SELECT * FROM fee_rates WHERE fee_type = ?', (fee_type,)).fetchone()
    if not existing:
        return jsonify({'error': '费用类型不存在'}), 404

    db.execute('''
        UPDATE fee_rates SET rate_value = ?, rate_unit = ?, calc_method = ?, note = ?, updated_at = CURRENT_TIMESTAMP
        WHERE fee_type = ?
    ''', (rate_value, rate_unit, calc_method, note, fee_type))
    db.commit()
    return jsonify({'ok': True, 'fee_type': fee_type, 'rate_value': rate_value})


# ===========================================================================
# 历史记录
# ===========================================================================
@app.route('/history')
@require_role('admin', 'director', 'liaison')
def history():
    db = get_db()
    page = int(request.args.get('page', 1))
    per_page = 50
    offset = (page - 1) * per_page

    # 从 item_submissions 查询
    total = db.execute('SELECT COUNT(*) as c FROM item_submissions').fetchone()['c']
    submissions = db.execute('''
        SELECT ist.*, ti.item_name, ti.task_config_id,
               tc.department_id, tc.settlement_type_id,
               d.name as dept_name, st.name as st_name
        FROM item_submissions ist
        JOIN task_items ti ON ist.task_item_id = ti.id
        JOIN task_configs tc ON ti.task_config_id = tc.id
        JOIN departments d ON tc.department_id = d.id
        JOIN settlement_types st ON tc.settlement_type_id = st.id
        ORDER BY ist.submitted_at DESC
        LIMIT ? OFFSET ?
    ''', (per_page, offset)).fetchall()

    total_pages = (total + per_page - 1) // per_page

    return render_template('history.html',
                           submissions=submissions,
                           page=page, total_pages=total_pages, total=total)


@app.route('/api/stats')
def api_stats():
    month = request.args.get('month', get_current_month())
    db = get_db()

    tasks = db.execute('''
        SELECT t.*, st.code as st_code, tc.id as task_config_id
        FROM tasks t
        JOIN settlement_types st ON t.settlement_type_id = st.id
        JOIN task_configs tc ON t.task_config_id = tc.id
        WHERE t.month = ?
    ''', (month,)).fetchall()

    total = len(tasks)
    completed = 0
    overdue = 0
    for t in tasks:
        # 直接读 tasks.status（由提交动作维护好的），不再发 COUNT 查询重算
        if t['status'] == 'completed':
            completed += 1
        if is_overdue(t):
            overdue += 1

    return jsonify({
        'month': month,
        'total': total,
        'completed': completed,
        'pending': total - completed,
        'overdue': overdue
    })


@app.route('/config/download-template')
def download_config_template():
    template_path = os.path.join(BASE_DIR, '部门结算任务配置模板.xlsx')
    if os.path.exists(template_path):
        return send_file(template_path, as_attachment=True, download_name='部门结算任务配置模板.xlsx')
    abort(404)


# ===========================================================================
# 系统状态 / 健康检查
# ===========================================================================
@app.route('/health')
def health_check():
    """健康检查接口，供监控用"""
    try:
        db = get_db()
        db.execute('SELECT 1')
        return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/system-status')
@require_role('admin')
def system_status():
    """系统状态页面：显示服务信息、备份情况、数据库统计"""
    import platform
    db = get_db()

    # 数据库统计
    stats = {}
    stats['departments'] = db.execute('SELECT COUNT(*) FROM departments').fetchone()[0]
    stats['task_configs'] = db.execute('SELECT COUNT(*) FROM task_configs WHERE is_active=1').fetchone()[0]
    stats['task_items'] = db.execute('SELECT COUNT(*) FROM task_items WHERE is_active=1').fetchone()[0]
    stats['templates'] = db.execute('SELECT COUNT(*) FROM template_files').fetchone()[0]
    stats['submissions'] = db.execute('SELECT COUNT(*) FROM item_submissions').fetchone()[0]

    # 备份信息
    backup_dir = os.path.join(DATA_DIR, 'backups')
    backups = []
    if os.path.exists(backup_dir):
        for f in sorted(os.listdir(backup_dir), reverse=True):
            if f.endswith('.db'):
                fpath = os.path.join(backup_dir, f)
                size_kb = os.path.getsize(fpath) / 1024
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                backups.append({
                    'name': f,
                    'size_kb': round(size_kb, 1),
                    'date': mtime.strftime('%Y-%m-%d %H:%M')
                })
    stats['backup_count'] = len(backups)

    # 数据库文件大小
    db_size = 0
    if os.path.exists(DB_PATH):
        db_size = os.path.getsize(DB_PATH) / 1024 / 1024

    # 上传文件统计
    upload_count = 0
    upload_size = 0
    if os.path.exists(UPLOAD_DIR):
        for f in os.listdir(UPLOAD_DIR):
            fpath = os.path.join(UPLOAD_DIR, f)
            if os.path.isfile(fpath):
                upload_count += 1
                upload_size += os.path.getsize(fpath)
    upload_size_mb = round(upload_size / 1024 / 1024, 1)

    return render_template('system_status.html',
                           stats=stats,
                           backups=backups[:10],
                           db_size_mb=round(db_size, 2),
                           upload_count=upload_count,
                           upload_size_mb=upload_size_mb,
                           python_version=platform.python_version(),
                           server_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


@app.route('/api/backup-now', methods=['POST'])
def backup_now():
    """手动触发数据库备份"""
    if USE_POSTGRES:
        return jsonify({'success': True, 'message': '云端数据库由 Vercel 自动备份，无需手动操作'})
    import shutil
    if not os.path.exists(DB_PATH):
        return jsonify({'success': False, 'message': '数据库文件不存在'}), 404
    backup_dir = os.path.join(DATA_DIR, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    today = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    backup_file = os.path.join(backup_dir, f'settlement_{today}.db')
    shutil.copy2(DB_PATH, backup_file)
    size_kb = os.path.getsize(backup_file) / 1024
    return jsonify({
        'success': True,
        'message': f'备份完成: settlement_{today}.db ({round(size_kb, 1)} KB)'
    })


# ===========================================================================
# 登录权限系统 + 对上对下结算金额统计 + 部门文件管理
# ===========================================================================
# 内置用户（密码与"部门权限密码表.xlsx"严格对应）
# 角色权限：
#   leader    - 项目领导班子（查阅下载，登录后默认跳转到 /settlement）
#   admin     - 系统管理员（全权：可查阅/下载/上传/修改/删除，含对上对下结算）
#   director  - 各部门部门主任（仅查阅本部门文件，不允许上传）
#   liaison   - 各部门联络人（上传/查阅/删除本部门文件）
BUILTIN_USERS = [
    # 1. 项目领导班子
    {'username': 'leader', 'display_name': '项目领导班子', 'password': 'pakil123456', 'role': 'leader', 'department': '项目领导', 'phone': ''},
    # 2. 系统管理员
    {'username': 'admin', 'display_name': '系统管理员', 'password': 'htglb888', 'role': 'admin', 'department': '合同管理部', 'phone': ''},
    # 3. 各部门主任（严格按 Excel 密码表）
    {'username': 'director_scdd', 'display_name': '生产调度部主任', 'password': 'scddb111', 'role': 'director', 'department': '生产调度部', 'phone': ''},
    {'username': 'director_gczljs', 'display_name': '工程质量技术部主任', 'password': 'gczljsb222', 'role': 'director', 'department': '工程质量技术部', 'phone': ''},
    {'username': 'director_aqhbb', 'display_name': '安全环保部主任', 'password': 'aqhbb333', 'role': 'director', 'department': '安全环保部', 'phone': ''},
    {'username': 'director_sbwzb', 'display_name': '设备物资部主任', 'password': 'sbwzb444', 'role': 'director', 'department': '设备物资部', 'phone': ''},
    # 2026-08-13：人力资源部已并入综合办公室，HR 账号删除（director_rlzyb/liaison_rlzyb）
    {'username': 'director_zhbgs', 'display_name': '综合办公室领导', 'password': 'zhbgs666', 'role': 'director', 'department': '综合办公室', 'phone': ''},
    # 4. 各部门联络人（密码按用户提供，2026-08-05 部署）
    {'username': 'liaison_scdd', 'display_name': '生产调度部联络人', 'password': 'scddbabc', 'role': 'liaison', 'department': '生产调度部', 'phone': ''},
    {'username': 'liaison_gczljs', 'display_name': '工程质量技术部联络人', 'password': 'gczljsbdef', 'role': 'liaison', 'department': '工程质量技术部', 'phone': ''},
    {'username': 'liaison_aqhbb', 'display_name': '安全环保部联络人', 'password': 'aqhbbghi', 'role': 'liaison', 'department': '安全环保部', 'phone': ''},
    {'username': 'liaison_sbwzb', 'display_name': '设备物资部联络人', 'password': 'sbwzbjkl', 'role': 'liaison', 'department': '设备物资部', 'phone': ''},
    # 2026-08-13：HR 联络人并入综合办公室后删除
    {'username': 'liaison_zhbgs', 'display_name': '综合办公室联络人', 'password': 'zhbgspqr', 'role': 'liaison', 'department': '综合办公室', 'phone': ''},
]

# 已注册部门列表（用于前端下拉）
DEPARTMENT_LIST = [
    '生产调度部', '工程质量技术部', '安全环保部', '设备物资部',
    '综合办公室', '合同管理部',
    '项目领导',
]

# 用户会话：token -> 用户信息 + 过期时间
USER_SESSION_TTL = 8 * 3600  # 8 小时

# 本地进程内缓存（避免每次请求都打 DB）
# Vercel Serverless 容器复用周期内有效；缓存命中可省一次 DB roundtrip
_session_cache = {}  # {token: (user_dict, expire_ts)}
_SESSION_CACHE_TTL = 30  # 缓存 30 秒（足够覆盖一连串导航请求）


def generate_token():
    """生成 32 字节 URL-safe 随机 token"""
    return secrets.token_urlsafe(32)


def parse_token(token):
    """解析 token，返回用户信息或 None（持久化：user_sessions 表）

    - Vercel Serverless 每次冷启动会清空内存，故 session 必须落库
    - 加一层进程内缓存避免每次请求都打 DB
    - 命中 DB 后做"滑动续期"（UPDATE expire_ts）
    """
    if not token:
        return None

    # 1. 进程内缓存命中
    now = time.time()
    cached = _session_cache.get(token)
    if cached and cached[1] > now:
        return cached[0]

    # 2. 查 DB
    try:
        db = get_db()
        row = db.execute(
            'SELECT username, display_name, role, department, phone, expire_ts '
            'FROM user_sessions WHERE token = ?',
            (token,)
        ).fetchone()
    except Exception as e:
        print(f'[parse_token] db error: {e}')
        return None

    if not row:
        _session_cache.pop(token, None)
        return None

    if float(row['expire_ts']) < now:
        # 过期，主动清理
        try:
            db.execute('DELETE FROM user_sessions WHERE token = ?', (token,))
            db.commit()
        except Exception:
            pass
        _session_cache.pop(token, None)
        return None

    user = {
        'username': row['username'],
        'display_name': row['display_name'],
        'role': row['role'],
        'department': row['department'] or '',
        'phone': row['phone'] or '',
    }

    # 3. 滑动续期（异步写不阻塞请求路径）
    new_expire = now + USER_SESSION_TTL
    try:
        db.execute('UPDATE user_sessions SET expire_ts = ? WHERE token = ?',
                   (new_expire, token))
        db.commit()
    except Exception as e:
        # 续期失败不影响本次登录态判断
        print(f'[parse_token] sliding expire failed: {e}')

    _session_cache[token] = (user, new_expire)
    return user


def save_session(token, user_dict):
    """把 session 写到 DB（兼容 SQLite / PostgreSQL）"""
    db = get_db()
    expire_ts = time.time() + USER_SESSION_TTL
    # 用 INSERT OR IGNORE / ON CONFLICT 避免重复 token（极小概率）
    if USE_POSTGRES:
        db.execute(
            'INSERT INTO user_sessions (token, username, display_name, role, department, phone, expire_ts) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s) '
            'ON CONFLICT (token) DO UPDATE SET expire_ts = EXCLUDED.expire_ts',
            (token, user_dict['username'], user_dict['display_name'],
             user_dict['role'], user_dict.get('department') or '',
             user_dict.get('phone') or '', expire_ts)
        )
    else:
        db.execute(
            'INSERT OR REPLACE INTO user_sessions (token, username, display_name, role, department, phone, expire_ts) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (token, user_dict['username'], user_dict['display_name'],
             user_dict['role'], user_dict.get('department') or '',
             user_dict.get('phone') or '', expire_ts)
        )
    db.commit()
    _session_cache[token] = (
        {
            'username': user_dict['username'],
            'display_name': user_dict['display_name'],
            'role': user_dict['role'],
            'department': user_dict.get('department') or '',
            'phone': user_dict.get('phone') or '',
        },
        expire_ts,
    )


def delete_session(token):
    """从 DB 删 session"""
    if not token:
        return
    try:
        db = get_db()
        if USE_POSTGRES:
            db.execute('DELETE FROM user_sessions WHERE token = %s', (token,))
        else:
            db.execute('DELETE FROM user_sessions WHERE token = ?', (token,))
        db.commit()
    except Exception as e:
        print(f'[delete_session] db error: {e}')
    _session_cache.pop(token, None)


def get_current_user():
    """从请求中获取当前登录用户"""
    token = (request.headers.get('X-Auth-Token')
             or request.form.get('auth_token')
             or request.args.get('auth_token')
             or request.cookies.get('auth_token'))
    return parse_token(token)


def seed_users(db):
    """初始化内置用户。

    策略：
      - 如果 username 不存在 → INSERT
      - 如果 username 已存在但 password 不同 → UPDATE（覆盖密码以反映 Excel 表的最新约定）
      - 其它字段（display_name / role / department）始终同步到 BUILTIN_USERS
      - 清理 BUILTIN_USERS 不再包含的旧 username（如旧的 director_zlyg 等已被新名替换）

    这样历史部署已经 seed 过旧版用户（比如 director_zlyg → 工程质量技术部/zlygb222），
    重新部署后会按最新的 Excel 表自动校正密码、部门、display_name，并删除残留旧账号。
    """
    builtin_usernames = {u['username'] for u in BUILTIN_USERS}

    for u in BUILTIN_USERS:
        try:
            existing = db.execute(
                'SELECT id, password, display_name, role, department, phone FROM users WHERE username = ?',
                (u['username'],)
            ).fetchone()
            if existing is None:
                db.execute('''
                    INSERT INTO users (username, display_name, password, role, department, phone)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (u['username'], u['display_name'], u['password'], u['role'], u['department'], u['phone']))
            else:
                # 同步密码、display_name、role、department；phone 保留旧值（除非为空）
                new_phone = u['phone'] or existing['phone'] or ''
                db.execute('''
                    UPDATE users
                    SET password = ?, display_name = ?, role = ?, department = ?, phone = ?
                    WHERE username = ?
                ''', (u['password'], u['display_name'], u['role'], u['department'], new_phone, u['username']))
        except Exception as e:
            print(f'[seed_users] upsert skip {u["username"]}: {e}')

    # 清理已被新账号替换的残留旧账号（仅当 username 不在 BUILTIN_USERS 时删除）
    try:
        existing_rows = db.execute('SELECT id, username FROM users').fetchall()
        for r in existing_rows:
            if r['username'] not in builtin_usernames:
                db.execute('DELETE FROM users WHERE id = ?', (r['id'],))
                print(f'[seed_users] pruned legacy user: {r["username"]}')
    except Exception as e:
        print(f'[seed_users] prune legacy: {e}')

    db.commit()


# ---------- 登录 / 登出 ----------
@app.route('/login')
def login_page():
    """登录页"""
    if get_current_user():
        return redirect(url_for('dashboard'))
    # 参与部门数 = 部门表中除"财务资金部"外的部门数
    # （财务资金部仅做收款台账/发票，不参与月度上报，故不计入参与部门）
    db = get_db()
    dept_count = db.execute(
        "SELECT COUNT(*) FROM departments WHERE name != '财务资金部'"
    ).fetchone()[0]
    return render_template('login.html', departments=DEPARTMENT_LIST, dept_count=dept_count)


def _post_login_redirect(role, requested_next):
    """根据角色决定登录后默认跳转目标。

    规则：
      - leader  → /            （仪表盘，查看排行榜）
      - 其他    → /            （仪表盘）
    requested_next 参数若合法则优先采用（如 /login?next=/submit）。
    """
    if requested_next and requested_next.startswith('/') and not requested_next.startswith('//'):
        return requested_next
    return url_for('dashboard')


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """登录：username + password"""
    db = get_db()
    username = (request.form.get('username') or '').strip()
    password = (request.form.get('password') or '').strip()
    role_hint = (request.form.get('role') or '').strip()
    department = (request.form.get('department') or '').strip()

    if not username or not password:
        return jsonify({'error': '请输入账号和密码'}), 400

    # 优先从数据库查（兼容用户改过密码的场景）
    user = db.execute(
        'SELECT * FROM users WHERE username = ?', (username,)
    ).fetchone()

    if not user:
        return jsonify({'error': '账号不存在'}), 401
    if user['password'] != password:
        return jsonify({'error': '密码错误'}), 401

    user_dict = dict(user)
    # 生成 token
    token = generate_token()
    save_session(token, user_dict)

    # 角色校验：director/liaison 必须匹配 department
    if user_dict['role'] in ('director', 'liaison') and department:
        if user_dict.get('department') and user_dict['department'] != department:
            return jsonify({'error': f'该账号属于「{user_dict["department"]}」，与所选部门不一致'}), 403

    next_path = _post_login_redirect(user_dict['role'], request.form.get('next') or request.args.get('next'))

    return jsonify({
        'ok': True,
        'token': token,
        'next': next_path,
        'user': {
            'id': user_dict['id'],
            'username': user_dict['username'],
            'display_name': user_dict['display_name'],
            'role': user_dict['role'],
            'department': user_dict.get('department', ''),
            'phone': user_dict.get('phone', ''),
        },
        'expires_in': USER_SESSION_TTL,
    })


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    """登出：清掉 token（兼容 header/form/cookie 三种来源）"""
    token = (request.headers.get('X-Auth-Token')
             or request.form.get('auth_token')
             or request.cookies.get('auth_token'))
    if token:
        delete_session(token)
    # 清除浏览器 cookie（让前端即使 token 失效也能登出）
    resp = jsonify({'ok': True, 'message': '已退出登录'})
    resp.set_cookie('auth_token', '', max_age=0, path='/')
    return resp


@app.route('/api/auth/logout-ui', methods=['POST'])
def api_auth_logout_ui():
    """表单退出（页面按钮触发，跳转回登录页）"""
    token = (request.headers.get('X-Auth-Token')
             or request.form.get('auth_token')
             or request.cookies.get('auth_token'))
    if token:
        delete_session(token)
    resp = redirect(url_for('login_page'))
    # 强制清除浏览器 cookie
    resp.set_cookie('auth_token', '', max_age=0, path='/')
    return resp


@app.route('/api/auth/me')
def api_auth_me():
    """当前用户信息"""
    user = get_current_user()
    if not user:
        return jsonify({'ok': False}), 401
    return jsonify({'ok': True, 'user': user})


@app.route('/api/auth/departments')
def api_auth_departments():
    """部门列表（公开，供登录页下拉用）"""
    return jsonify({'ok': True, 'departments': DEPARTMENT_LIST})


@app.route('/api/auth/users')
@require_role('admin')
def api_auth_users():
    """用户列表（仅管理员可见）"""
    db = get_db()
    users = db.execute('SELECT id, username, display_name, role, department, phone, created_at FROM users ORDER BY id').fetchall()
    return jsonify([dict(u) for u in users])


# ---------- 对上对下结算金额统计 ----------
@app.route('/settlement')
@require_role('leader', 'admin')
def settlement_page():
    """结算金额统计页 - 仅项目领导班子 + 系统管理员可见"""
    return render_template('settlement.html')


@app.route('/api/settlement-records', methods=['GET'])
@require_role('leader', 'admin')
def api_list_settlement_records():
    """查询结算金额记录（领导班子 + 管理员可见全部，director/liaison 无权访问）"""
    db = get_db()
    user = g.current_user

    direction = request.args.get('direction', '')  # up / down
    status = request.args.get('status', '')
    project = request.args.get('project', '').strip()
    department_filter = request.args.get('department', '').strip()

    query = '''
        SELECT sr.id, sr.direction, sr.project_name, sr.counterparty, sr.contract_no,
               sr.amount, sr.currency, sr.settle_date, sr.settle_month, sr.status, sr.notes,
               sr.attachment_name, sr.attachment_size, sr.created_by, sr.created_at, sr.updated_at,
               sa.currency as amt_currency, sa.amount as amt_amount, sa.sort_order
        FROM settlement_records sr
        LEFT JOIN settlement_amounts sa ON sr.id = sa.settlement_record_id
        WHERE 1=1
    '''
    params = []
    if direction in ('up', 'down'):
        query += ' AND sr.direction = ?'
        params.append(direction)
    if status:
        query += ' AND sr.status = ?'
        params.append(status)
    if project:
        query += ' AND sr.project_name LIKE ?'
        params.append(f'%{project}%')
    if department_filter:
        query += ' AND (sr.created_by LIKE ? OR sr.notes LIKE ?)'
        params.extend([f'%{department_filter}%', f'%{department_filter}%'])

    # 访问角色：仅 leader + admin（按 @require_role 拦截）
    # 此处不再追加 director/liaison 部门过滤

    query += ' ORDER BY sr.settle_date DESC, sr.id DESC, sa.sort_order'
    rows = db.execute(query, params).fetchall()

    records_map = {}
    for r in rows:
        rid = r['id']
        if rid not in records_map:
            d = {
                'id': rid,
                'direction': r['direction'],
                'project_name': r['project_name'],
                'counterparty': r['counterparty'],
                'contract_no': r['contract_no'],
                'amount': float(r['amount']) if r['amount'] is not None else 0.0,
                'currency': r['currency'] or 'PHP',
                'settle_date': r['settle_date'],
                'settle_month': r['settle_month'] or '',
                'status': r['status'],
                'notes': r['notes'],
                'attachment_name': r['attachment_name'],
                'attachment_size': r['attachment_size'],
                'created_by': r['created_by'],
                'created_at': r['created_at'],
                'updated_at': r['updated_at'],
                'amounts': [],
                'attachments': [],
            }
            records_map[rid] = d
        if r['amt_currency'] is not None:
            records_map[rid]['amounts'].append({
                'currency': r['amt_currency'],
                'amount': float(r['amt_amount']) if r['amt_amount'] is not None else 0.0,
            })

    # 批量查询附件
    if records_map:
        placeholders = ','.join('?' * len(records_map))
        att_rows = db.execute(
            f'SELECT id, settlement_record_id, file_name, file_size FROM settlement_attachments WHERE settlement_record_id IN ({placeholders}) ORDER BY id',
            list(records_map.keys())
        ).fetchall()
        for ar in att_rows:
            rec = records_map.get(ar['settlement_record_id'])
            if rec:
                rec['attachments'].append({
                    'id': ar['id'],
                    'file_name': ar['file_name'],
                    'file_size': ar['file_size'],
                })

    # 兼容无子表金额的旧数据：退回到主表 amount/currency
    records = []
    for d in records_map.values():
        if not d['amounts']:
            d['amounts'] = [{'currency': d['currency'], 'amount': d['amount']}]
        records.append(d)
    return jsonify({'ok': True, 'records': records})


@app.route('/api/settlement-records/summary')
@require_role('leader', 'admin')
def api_settlement_summary():
    """汇总统计卡（对上/对下 按币种分组：累计金额、已完成、办理中、本月新增）
    访问角色：仅 leader + admin（按 @require_role 拦截），不再追加部门过滤。
    """
    db = get_db()
    month_prefix = datetime.now().strftime('%Y-%m')

    def sum_by_currency(sql, params):
        try:
            rows = db.execute(sql, params).fetchall()
        except Exception:
            rows = []
        out = {}
        for r in rows:
            cur = (r['currency'] or 'PHP')
            out[cur] = {'amount': float(r['total']), 'count': r['cnt']}
        return out

    def query_total(direction):
        return sum_by_currency(
            "SELECT COALESCE(sa.currency, 'PHP') as currency, COALESCE(SUM(sa.amount), 0) as total, "
            "COUNT(DISTINCT sr.id) as cnt "
            "FROM settlement_records sr "
            "LEFT JOIN settlement_amounts sa ON sr.id = sa.settlement_record_id "
            "WHERE sr.direction = ? "
            "GROUP BY COALESCE(sa.currency, 'PHP')",
            [direction]
        )

    def query_status(direction, *statuses):
        placeholders = ','.join('?' * len(statuses))
        return sum_by_currency(
            "SELECT COALESCE(sa.currency, 'PHP') as currency, COALESCE(SUM(sa.amount), 0) as total, "
            "COUNT(DISTINCT sr.id) as cnt "
            "FROM settlement_records sr "
            "LEFT JOIN settlement_amounts sa ON sr.id = sa.settlement_record_id "
            f"WHERE sr.direction = ? AND sr.status IN ({placeholders}) "
            "GROUP BY COALESCE(sa.currency, 'PHP')",
            [direction] + list(statuses)
        )

    def query_month(direction):
        return sum_by_currency(
            "SELECT COALESCE(sa.currency, 'PHP') as currency, COALESCE(SUM(sa.amount), 0) as total, "
            "COUNT(DISTINCT sr.id) as cnt "
            "FROM settlement_records sr "
            "LEFT JOIN settlement_amounts sa ON sr.id = sa.settlement_record_id "
            "WHERE sr.direction = ? AND (sr.settle_month = ? OR sr.settle_date LIKE ?) "
            "GROUP BY COALESCE(sa.currency, 'PHP')",
            [direction, month_prefix, f'{month_prefix}%']
        )

    # 未付款(unpaid) 也视为办理中口径
    pending_statuses = ('pending', 'processing', 'unpaid')

    return jsonify({
        'ok': True,
        'upstream': {
            'total': query_total('up'),
            'completed': query_status('up', 'completed'),
            'pending': query_status('up', *pending_statuses),
            'month': query_month('up'),
        },
        'downstream': {
            'total': query_total('down'),
            'completed': query_status('down', 'completed'),
            'pending': query_status('down', *pending_statuses),
            'month': query_month('down'),
        },
    })


_AMOUNT_RE = re.compile(r'^amounts\[(\d+)\]\[(currency|amount)\]$')


def parse_amounts_from_form(form):
    """从 FormData 解析多币种金额，例如 amounts[0][currency]=PHP&amounts[0][amount]=100。"""
    raw = {}
    for key, value in form.items():
        m = _AMOUNT_RE.match(key)
        if not m:
            continue
        idx = int(m.group(1))
        field = m.group(2)
        raw.setdefault(idx, {})[field] = (value or '').strip()

    result = []
    for idx in sorted(raw.keys()):
        item = raw[idx]
        if 'amount' not in item:
            continue
        try:
            amt = float(item['amount'])
        except (ValueError, TypeError):
            continue
        if amt < 0:
            continue
        result.append({
            'currency': (item.get('currency') or 'PHP').strip(),
            'amount': amt,
        })
    return result


def save_settlement_amounts(db, record_id, amounts):
    """保存结算单的多币种金额：先清后插。"""
    db.execute('DELETE FROM settlement_amounts WHERE settlement_record_id = ?', (record_id,))
    for i, a in enumerate(amounts):
        db.execute(
            'INSERT INTO settlement_amounts (settlement_record_id, currency, amount, sort_order) VALUES (?, ?, ?, ?)',
            (record_id, a['currency'], a['amount'], i)
        )


def save_settlement_attachments(db, record_id, files):
    """保存结算单的多附件：追加模式。本地存磁盘，Vercel 存 BYTEA，启用 Blob 时存 URL。"""
    for file in files:
        if not file or not file.filename:
            continue
        if not allowed_file(file.filename):
            continue
        stored, size, original, data, blob_url, blob_pathname = save_upload_file(file, subdir='settlement_attachments')
        db.execute('''
            INSERT INTO settlement_attachments (settlement_record_id, file_name, stored_name, file_data, file_size, blob_url, blob_pathname)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (record_id, original, stored, data, size, blob_url, blob_pathname))


@app.route('/api/settlement-records', methods=['POST'])
@require_role('admin')
def api_create_settlement_record():
    """新建结算金额记录（仅管理员），支持一条记录多币种金额。"""
    db = get_db()
    user = g.current_user

    direction = (request.form.get('direction') or '').strip()
    project_name = (request.form.get('project_name') or '').strip()
    counterparty = (request.form.get('counterparty') or '').strip()
    contract_no = (request.form.get('contract_no') or '').strip()
    settle_month = (request.form.get('settle_month') or '').strip()
    status = (request.form.get('status') or 'pending').strip()
    notes = (request.form.get('notes') or '').strip()
    department_tag = (request.form.get('department') or user.get('department', '')).strip()

    if direction not in ('up', 'down'):
        return jsonify({'error': '结算方向必须为 up 或 down'}), 400
    if not project_name:
        return jsonify({'error': '请填写项目名称'}), 400
    if not counterparty:
        return jsonify({'error': '请填写对方单位'}), 400

    amounts = parse_amounts_from_form(request.form)
    # 兼容旧客户端：未传 amounts[] 时使用单币种字段
    if not amounts:
        amount_raw = (request.form.get('amount') or '0').strip()
        currency = (request.form.get('currency') or 'PHP').strip()
        try:
            amount = float(amount_raw)
        except ValueError:
            return jsonify({'error': '金额必须是数字'}), 400
        if amount < 0:
            return jsonify({'error': '金额不能为负数'}), 400
        amounts = [{'currency': currency, 'amount': amount}]

    if not amounts:
        return jsonify({'error': '请至少填写一笔金额'}), 400

    primary = amounts[0]

    # 多附件上传（新前端使用 attachments[]）
    uploaded_files = request.files.getlist('attachments')
    # 兼容旧客户端单附件字段
    single_file = request.files.get('attachment')
    if single_file and single_file.filename:
        uploaded_files.insert(0, single_file)

    created_by = f"{user['display_name']}({user['role']})"
    # 把部门信息写入 notes，便于按部门过滤（director/liaison 的过滤查询依赖此字段）
    if department_tag and department_tag not in notes:
        notes = f"[{department_tag}] {notes}".strip()

    # 为了兼容旧统计/排序，结算月份也回填到 settle_date（当月首日）
    settle_date = None
    if settle_month:
        settle_date = f"{settle_month}-01"

    if USE_POSTGRES:
        new_id = insert_returning_id(db, '''
            INSERT INTO settlement_records
            (direction, project_name, counterparty, contract_no, amount, currency, settle_date, settle_month, status, notes, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (direction, project_name, counterparty, contract_no, primary['amount'], primary['currency'],
              settle_date, settle_month or None, status, notes, created_by))
    else:
        db.execute('''
            INSERT INTO settlement_records
            (direction, project_name, counterparty, contract_no, amount, currency, settle_date, settle_month, status, notes, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (direction, project_name, counterparty, contract_no, primary['amount'], primary['currency'],
              settle_date, settle_month or None, status, notes, created_by))
        new_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    save_settlement_amounts(db, new_id, amounts)
    save_settlement_attachments(db, new_id, uploaded_files)
    db.commit()
    return jsonify({'ok': True, 'id': new_id})


@app.route('/api/settlement-records/<int:rid>', methods=['PUT'])
@require_role('admin')
def api_update_settlement_record(rid):
    """更新结算金额记录（仅管理员）"""
    db = get_db()
    user = g.current_user

    row = db.execute('SELECT * FROM settlement_records WHERE id = ?', (rid,)).fetchone()
    if not row:
        return jsonify({'error': '记录不存在'}), 404

    try:
        direction = (request.form.get('direction') or row['direction']).strip()
        project_name = (request.form.get('project_name') or row['project_name']).strip()
        counterparty = (request.form.get('counterparty') or row['counterparty']).strip()
        contract_no = (request.form.get('contract_no') or row['contract_no'] or '').strip()
        settle_month = (request.form.get('settle_month') or row['settle_month'] or '').strip()
        status = (request.form.get('status') or row['status'] or 'pending').strip()
        notes = (request.form.get('notes') or row['notes'] or '').strip()

        amounts = parse_amounts_from_form(request.form)
        # 兼容旧客户端：未传 amounts[] 时使用单币种字段
        if not amounts:
            amount_raw = request.form.get('amount', str(row['amount'] or 0))
            currency = (request.form.get('currency') or row['currency'] or 'PHP').strip()
            try:
                amount = float(amount_raw)
            except ValueError:
                return jsonify({'error': '金额必须是数字'}), 400
            if amount < 0:
                return jsonify({'error': '金额不能为负数'}), 400
            amounts = [{'currency': currency, 'amount': amount}]

        if not amounts:
            return jsonify({'error': '请至少填写一笔金额'}), 400

        primary = amounts[0]

        # 结算月份回填到 settle_date（当月首日），兼容旧统计
        settle_date = None
        if settle_month:
            settle_date = f"{settle_month}-01"
        elif row['settle_date']:
            settle_date = row['settle_date']

        # 多附件上传：追加模式
        uploaded_files = request.files.getlist('attachments')
        single_file = request.files.get('attachment')
        if single_file and single_file.filename:
            uploaded_files.insert(0, single_file)

        if USE_POSTGRES:
            db.execute('''
                UPDATE settlement_records
                SET direction=?, project_name=?, counterparty=?, contract_no=?, amount=?, currency=?,
                    settle_date=?, settle_month=?, status=?, notes=?,
                    attachment_name=NULL, attachment_stored=NULL, attachment_data=NULL, attachment_size=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            ''', (direction, project_name, counterparty, contract_no, primary['amount'], primary['currency'],
                  settle_date, settle_month or None, status, notes, rid))
        else:
            db.execute('''
                UPDATE settlement_records
                SET direction=?, project_name=?, counterparty=?, contract_no=?, amount=?, currency=?,
                    settle_date=?, settle_month=?, status=?, notes=?,
                    attachment_name=NULL, attachment_stored=NULL, attachment_size=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            ''', (direction, project_name, counterparty, contract_no, primary['amount'], primary['currency'],
                  settle_date, settle_month or None, status, notes, rid))

        save_settlement_amounts(db, rid, amounts)
        save_settlement_attachments(db, rid, uploaded_files)
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        print(f'[api_update_settlement_record] error: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'保存失败：{str(e)}'}), 500


@app.route('/api/settlement-records/<int:rid>', methods=['DELETE'])
@require_role('admin')
def api_delete_settlement_record(rid):
    """删除结算金额记录（仅管理员）"""
    db = get_db()
    row = db.execute('SELECT * FROM settlement_records WHERE id = ?', (rid,)).fetchone()
    if not row:
        return jsonify({'error': '记录不存在'}), 404
    db.execute('DELETE FROM settlement_records WHERE id = ?', (rid,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/download/settlement-attachment/<int:aid>')
@require_role('leader', 'admin')
def download_settlement_attachment(aid):
    """下载结算金额附件（按 settlement_attachments.id）"""
    db = get_db()
    row = db.execute('SELECT * FROM settlement_attachments WHERE id = ?', (aid,)).fetchone()
    if not row:
        abort(404)
    # 启用 Blob 时直接重定向到对象存储 URL（鉴权已在路由层完成）
    if row['blob_pathname'] and row['blob_url']:
        return redirect(row['blob_url'])
    if USE_POSTGRES:
        if not row['file_data']:
            abort(404)
        return send_file(io.BytesIO(row['file_data']), as_attachment=True, download_name=row['file_name'])
    filepath = os.path.join(UPLOAD_DIR, 'settlement_attachments', row['stored_name'])
    if not os.path.exists(filepath):
        abort(404)
    return send_file(filepath, as_attachment=True, download_name=row['file_name'])


@app.route('/api/settlement-attachment/<int:aid>', methods=['DELETE'])
@require_role('admin')
def api_delete_settlement_attachment(aid):
    """删除结算单的某个附件（仅管理员可操作）。"""
    db = get_db()
    row = db.execute('SELECT * FROM settlement_attachments WHERE id = ?', (aid,)).fetchone()
    if not row:
        return jsonify({'error': '附件不存在'}), 404
    if not USE_POSTGRES:
        filepath = os.path.join(UPLOAD_DIR, 'settlement_attachments', row['stored_name'])
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
    db.execute('DELETE FROM settlement_attachments WHERE id = ?', (aid,))
    db.commit()
    return jsonify({'ok': True})


# 部门文件管理模块已于 2026-08-13 下线（用户要求"不用再加部门文件模块"）。
# 5 个路由（/department-files 与 4 个 /api/department-files）已整体移除，department_files 表 schema 同步从 db.py 删除。
# 历史实现参见 git log 8e3f9af 等提交。

# ---------- 一次性迁移端点：HR → 综合办公室（已完成，2026-08-13 禁用） ----------
@app.route('/api/_run-merge-hr', methods=['GET'])
def api_run_merge_hr():
    """保留端点但禁用：合并已在 2026-08-13 完成。重复访问安全返回 410。"""
    user = get_current_user()
    if not user or user.get('role') != 'admin':
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    return jsonify({'ok': False, 'error': 'migration_already_done', 'message': 'HR 合并已在 2026-08-13 完成，本端点已禁用。如需重新启用请参见 git log 2f14327/271c2f5 的迁移实现。'}), 410


# ---------- 一次性同步：把 task_configs.required_materials 刷新到当前/历史月份 tasks 行（已完成，2026-08-13 禁用） ----------
# 端点已执行完成（unmatched_tasks_after_sync=0）。为避免被误调造成覆盖，路由永久返回 410。
# 历史实现与执行结果参见 git log cca6d2c / 8b06235；如需重新启用请从 git 历史恢复。
@app.route('/api/_sync-tasks-materials', methods=['GET'])
def api_sync_tasks_materials():
    """已禁用。合并后冷启动已经存在的 tasks 行持有合并前的 required_materials 快照；该同步已在 2026-08-13 执行完成（configs_synced=2, unmatched_tasks_after_sync=0），本端点永久 410。"""
    user = get_current_user()
    if not user or user.get('role') != 'admin':
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    return jsonify({
        'ok': False,
        'error': 'sync_already_done',
        'message': 'required_materials 同步已在 2026-08-13 完成，本端点已禁用。'
    }), 410


# ===========================================================================
# 错误处理
# ===========================================================================
@app.errorhandler(413)
def too_large(e):
    flash('文件太大，最大支持100MB', 'danger')
    return redirect(request.url), 413


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, message='页面不存在'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', code=500, message='服务器内部错误'), 500


@app.context_processor
def inject_globals():
    return {
        'app_title': '帕基尔项目月度结算管理系统',
        'current_month': get_current_month(),
        'static_url': static_url,  # 注册到 Jinja globals，import 出来的 macro 也能用
        'ICON_SYMBOLS': ICON_SYMBOLS,  # 模板可按需取 <symbol> 字符串
    }


# ===========================================================================
# 静态资源 URL 自动加内容 hash（基于文件 mtime + size，前 8 字符）。
# 配合 vercel.json 的 `immutable, max-age=2592000`（30天）实现：
# - 首次访问：浏览器下载并缓存 30 天
# - 二次访问：304/命中本地缓存，零网络请求
# - 文件更新后：hash 变化 → URL 变化 → 浏览器拉新版，旧版继续缓存不污染
# 必须在 inject_globals 之前定义（被它引用），并注册到 jinja_env.globals 让
# {% import '_macros.html' as ui %} 后的宏也能解析到。
# ===========================================================================
import hashlib as _hashlib
from flask import url_for as _url_for
_STATIC_ROOT = os.path.join(app.static_folder) if app.static_folder else None


def static_url(filename):
    try:
        full = os.path.join(_STATIC_ROOT, filename) if _STATIC_ROOT else filename
        if os.path.isfile(full):
            st = os.stat(full)
            h = _hashlib.md5(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:8]
            return _url_for('static', filename=filename) + f'?v={h}'
    except Exception:
        pass
    return _url_for('static', filename=filename)


# 让 {% import %} 出来的宏也能用 static_url（context_processor 不会注入到 imported namespace）
app.jinja_env.globals['static_url'] = static_url


# ===========================================================================
# SVG 图标 sprite 按需内联（性能优化）
#   - 模板 ui.icon() 宏收集 name 到 g.requested_icons
#   - base.html 末尾只输出当前请求命中的 <symbol>，省去 1 个 RTT + 12KB icons.svg
#   - 解析 templates/_icons.html 在模块加载时一次性完成，预热到 ICON_SYMBOLS dict
# ===========================================================================
import re as _re_svg

def _load_icon_symbols():
    """从 templates/_icons.html 提取 {name: <symbol...>...</symbol>} 字典。
    返回值仅为 <symbol> 节点字符串（不含外层 <svg> 包装），可拼接到任意 <svg> 容器。
    """
    icons_path = os.path.join(BASE_DIR, 'templates', '_icons.html')
    if not os.path.isfile(icons_path):
        return {}
    try:
        with open(icons_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return {}
    # 提取每个 <symbol ... id="i-NAME">...</symbol>，name = NAME
    pattern = _re_svg.compile(
        r'<symbol\b[^>]*\bid=["\']i-([a-z0-9-]+)["\'][^>]*>.*?</symbol>',
        _re_svg.DOTALL | _re_svg.IGNORECASE
    )
    return {m.group(1): m.group(0) for m in pattern.finditer(content)}


ICON_SYMBOLS = _load_icon_symbols()


# ===========================================================================
# 性能优化：响应缓存头
#   - HTML：浏览器私有缓存 10s（连切页秒开，CDN 不缓存私有页）
#   - JSON API：不缓存（数据随时变）
#   - 文件下载（attachment）：不缓存
#   - /static/ 静态资源：public + 30天 immutable（依赖 static_url 加 hash，
#     文件更新后 URL 变化 → 旧版继续缓存 30 天不会污染）
# ===========================================================================
@app.after_request
def _perf_headers(response):
    ct = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()

    # 静态资源：在 Flask 路由层强制加 immutable 头（Vercel vercel.json 的
    # /static/(.*) headers 段在边缘 CDN 优先级不够，会被 Vercel 默认静态
    # 缓存策略覆盖）。这里直接覆盖以确保命中。
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=2592000, immutable'
        return response
    # /login 未登录页：浏览器不缓存，CDN 共享缓存 60s + SWR 5 分钟
    # （带 Cookie 的请求 Vercel 自动跳过 CDN 缓存，登录页本身无敏感数据，可共享）
    if request.path == '/login' and ct == 'text/html':
        response.headers['Cache-Control'] = 'public, s-maxage=60, stale-while-revalidate=300, max-age=0'
        return response
    # HTML 页面：浏览器私有缓存 10s
    if ct == 'text/html':
        response.headers.setdefault('Cache-Control', 'private, max-age=10')
    # JSON API：默认不缓存
    elif ct in ('application/json', 'text/json'):
        response.headers.setdefault('Cache-Control', 'no-store')
    # 文件下载（attachment）：不缓存
    elif 'attachment' in (response.headers.get('Content-Disposition') or ''):
        response.headers.setdefault('Cache-Control', 'no-store')

    return response


# ===========================================================================
# 启动
# ===========================================================================
if __name__ == '__main__':
    init_db()
    print('=' * 60)
    print('  帕基尔项目月度结算管理系统')
    print('  访问地址: http://localhost:5000')
    print('  局域网访问: http://<本机IP>:5000')
    print('=' * 60)
    try:
        from waitress import serve
        print('  [生产模式] 使用 waitress 服务器')
        serve(app, host='0.0.0.0', port=5000, threads=8)
    except ImportError:
        print('  [开发模式] 使用 Flask 内置服务器')
        app.run(host='0.0.0.0', port=5000, debug=False)


# 2026-08-05: 仪表盘所有角色（含领导班子）都展示"全部部门完成情况对比·排名榜"

