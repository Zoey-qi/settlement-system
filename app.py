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
from datetime import datetime, date
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, send_file, flash, g, abort
)
from werkzeug.utils import secure_filename
from db import connect, USE_POSTGRES, init_schema, seed_default_data, insert_returning_id

# ===========================================================================
# 配置
# ===========================================================================
IS_VERCEL = bool(os.environ.get('VERCEL'))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 配置管理密码会话：token -> 过期时间戳
# 30 分钟无活动自动失效（同一 token 在写操作时会刷新）
PASSWORD_SESSION_TTL = 30 * 60  # 秒
password_sessions = {}  # {token: expire_ts}

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

app = Flask(__name__)
app.config['SECRET_KEY'] = 'pakil-settlement-2025'
# Vercel Hobby 限制请求体 4.5MB；本地无限制
# 注：单次请求 4MB 仍较小，但够用。若需要大文件可改为流式上传（Vercel 受 serverless body 限制）。
if USE_POSTGRES:
    app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024  # 4MB (Vercel safe，含 PDF 等)
else:
    app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB (local)
app.config['JSON_AS_ASCII'] = False
if not USE_POSTGRES:
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True

ALLOWED_EXTENSIONS = {
    '.xlsx', '.xls', '.csv', '.doc', '.docx',
    '.pdf', '.jpg', '.jpeg', '.png', '.zip', '.rar',
    '.ppt', '.pptx', '.txt'
}

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


def user_can_view_dept(user, dept_name):
    """判断当前用户是否可查看指定部门数据。"""
    if not user:
        return False
    role = user['role']
    if role in ('admin', 'leader'):
        return True
    return dept_name == user.get('department', '')


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
    """将已有的 task_configs.required_materials 拆分为 task_items 条目"""
    configs = conn.execute('SELECT * FROM task_configs').fetchall()
    for cfg in configs:
        # 检查该 config 是否已有 items
        existing = conn.execute('SELECT COUNT(*) as c FROM task_items WHERE task_config_id = ?', (cfg['id'],)).fetchone()['c']
        if existing > 0:
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


def ensure_tasks_for_month(month_str):
    """确保某月的任务已生成"""
    db = get_db()
    configs = db.execute('''
        SELECT tc.*, d.name as dept_name, st.code as st_code, st.name as st_name
        FROM task_configs tc
        JOIN departments d ON tc.department_id = d.id
        JOIN settlement_types st ON tc.settlement_type_id = st.id
        WHERE tc.is_active = 1
    ''').fetchall()

    for cfg in configs:
        db.execute('''
            INSERT OR IGNORE INTO tasks
            (task_config_id, month, department_id, settlement_type_id, required_materials, deadline_day, status)
            VALUES (?,?,?,?,?,?, 'pending')
        ''', (cfg['id'], month_str, cfg['department_id'], cfg['settlement_type_id'],
              cfg['required_materials'], cfg['deadline_day']))

        # 更新任务状态基于条目提交情况
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


def save_upload_file(file, subdir='item_submissions'):
    """保存上传文件。本地存磁盘，Vercel 存内存（返回 bytes）"""
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    original = secure_filename(file.filename)
    if not original:
        original = 'unnamed_file'
    stored_name = f"{timestamp}_{original}"

    if USE_POSTGRES:
        # Vercel: 读取文件到内存
        file_data = file.read()
        file_size = len(file_data)
        return stored_name, file_size, original, file_data
    else:
        # 本地: 存到磁盘
        upload_dir = os.path.join(UPLOAD_DIR, subdir)
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, stored_name)
        file.save(filepath)
        file_size = os.path.getsize(filepath)
        return stored_name, file_size, original, None


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
    'api_purge_finance_2026',  # 一次性清理端点（secret 保护）
    'api_diag_finance_2026',   # 一次性诊断端点
}


# =========================================================================
# 一次性诊断端点：查看 tasks 表里所有 department_id 引用情况
# =========================================================================
@app.route('/api/_diag_finance_2026', methods=['GET'])
def api_diag_finance_2026():
    if request.args.get('secret') != 'diag-finance-2026':
        return jsonify(ok=False, error='invalid secret'), 403
    db = get_db()
    placeholder = '%s' if USE_POSTGRES else '?'

    # 1. 所有 departments
    depts = db.execute(f'SELECT id, name FROM departments ORDER BY id').fetchall()
    # 2. tasks 表里所有不重复的 department_id
    task_dept_ids = db.execute(f'SELECT DISTINCT department_id FROM tasks ORDER BY department_id').fetchall()
    # 3. 找出 tasks 里 department_id 在 departments 里找不到的（孤儿）
    orphans = db.execute(f'''
        SELECT t.id, t.task_config_id, t.department_id, t.month, t.status
        FROM tasks t
        LEFT JOIN departments d ON t.department_id = d.id
        WHERE d.id IS NULL
    ''').fetchall()
    # 4. 找出 task id=7/14 的详情
    t7 = db.execute(f'SELECT t.*, d.name as dept_name, tc.remarks FROM tasks t LEFT JOIN departments d ON t.department_id=d.id LEFT JOIN task_configs tc ON t.task_config_id=tc.id WHERE t.id IN (7,14)').fetchall()
    # 5. 所有 task_configs
    configs = db.execute(f'SELECT tc.id, tc.department_id, d.name as dept_name, tc.is_active FROM task_configs tc LEFT JOIN departments d ON tc.department_id=d.id ORDER BY tc.id').fetchall()

    return jsonify(
        ok=True,
        departments=[dict(d) for d in depts],
        task_distinct_dept_ids=[t['department_id'] for t in task_dept_ids],
        orphan_tasks=[dict(o) for o in orphans],
        task_id_7_14=[dict(t) for t in t7],
        all_task_configs=[dict(c) for c in configs],
    )


# =========================================================================
# 一次性远程修复端点（用完即删）
# 用途：清理 task_configs / task_items / tasks / departments 表中
#       name='财务资金部' 的所有残留数据。
# Secret: purge-finance-2026
# =========================================================================
@app.route('/api/_purge_finance_2026', methods=['GET'])
def api_purge_finance_2026():
    if request.args.get('secret') != 'purge-finance-2026':
        return jsonify(ok=False, error='invalid secret'), 403
    try:
        result = _do_purge_finance()
        return result
    except Exception as e:
        import traceback
        app.logger.error(f'PURGE FINANCE FAILED: {traceback.format_exc()}')
        return jsonify(
            ok=False,
            error=str(e),
            error_type=type(e).__name__,
            traceback_tail=traceback.format_exc().splitlines()[-15:],
        ), 500


def _do_purge_finance():
    db = get_db()
    target = '财务资金部'
    placeholder = '%s' if USE_POSTGRES else '?'

    def q(sql, params=()):
        cur = db.execute(sql, params)
        return cur

    # 1. 找出"财务资金部"部门的 id
    dept_row = q(f"SELECT id FROM departments WHERE name = {placeholder}", (target,)).fetchone()
    finance_dept_id = str(dept_row['id']) if dept_row else None
    dept_found = dept_row is not None

    # 2. 通过 department_id 找出所有 task_configs
    configs_deleted = 0
    items_deleted = 0
    tasks_deleted = 0
    dept_deleted_count = 0
    diag_configs_count = 0

    if finance_dept_id:
        rows = q(f"SELECT id FROM task_configs WHERE department_id = {placeholder}", (finance_dept_id,)).fetchall()
        finance_task_config_ids = [str(r['id']) for r in rows]
        diag_configs_count = len(finance_task_config_ids)

        # 删除顺序很重要，要按依赖关系：
        # item_submissions → task_items → task_configs → tasks → departments
        # (item_submissions.task_item_id → task_items.id → task_configs.id)

        # 3a. 找出所有关联的 task_item ids
        if finance_task_config_ids:
            placeholders_in = ','.join([placeholder] * len(finance_task_config_ids))
            item_rows = q(f"SELECT id FROM task_items WHERE task_config_id IN ({placeholders_in})", finance_task_config_ids).fetchall()
            finance_task_item_ids = [str(r['id']) for r in item_rows]

            # 3b. 删除 item_submissions（外键依赖 task_items）
            if finance_task_item_ids:
                placeholders_in2 = ','.join([placeholder] * len(finance_task_item_ids))
                cur = q(f"DELETE FROM item_submissions WHERE task_item_id IN ({placeholders_in2})", finance_task_item_ids)
                submissions_deleted = cur.rowcount
            else:
                submissions_deleted = 0

            # 3c. 删除 task_items
            for tc_id in finance_task_config_ids:
                cur = q(f"DELETE FROM task_items WHERE task_config_id = {placeholder}", (tc_id,))
                items_deleted += cur.rowcount

            # 4. 删除 task_configs
            for tc_id in finance_task_config_ids:
                cur = q(f"DELETE FROM task_configs WHERE id = {placeholder}", (tc_id,))
                configs_deleted += cur.rowcount
        else:
            submissions_deleted = 0

        # 5. 删除 tasks（按 department_id）
        cur = q(f"DELETE FROM tasks WHERE department_id = {placeholder}", (finance_dept_id,))
        tasks_deleted = cur.rowcount

        # 6. 删除 department 本身
        dept_cur = q(f"DELETE FROM departments WHERE id = {placeholder}", (finance_dept_id,))
        dept_deleted_count = dept_cur.rowcount

    db.commit()

    return jsonify(
        ok=True,
        message=f'已清理 财务资金部：departments({dept_deleted_count}) + task_configs({configs_deleted}) + task_items({items_deleted}) + item_submissions({submissions_deleted}) + tasks({tasks_deleted})',
        dept_found=dept_found,
        finance_dept_id=finance_dept_id,
        finance_task_configs_found=diag_configs_count,
    )


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


@app.route('/')
@require_auth
def dashboard():
    month = request.args.get('month', get_current_month())
    ensure_tasks_for_month(month)

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

    # 为每个任务附加条目信息
    task_list = []
    for t in tasks:
        items = get_items_with_status(t['task_config_id'], month)
        total_items = len(items)
        completed_items = sum(1 for i in items if i['sub_id'])
        task_dict = dict(t)
        task_dict['deadline_day'] = t['config_deadline_day']
        task_dict['items'] = items
        task_dict['total_items'] = total_items
        task_dict['completed_items'] = completed_items
        task_dict['status'] = compute_task_status(t['task_config_id'], month, db)
        task_list.append(task_dict)

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
                           my_dept_downstream=my_dept_downstream)


@app.route('/submit', methods=['GET'])
@require_auth
def submit():
    """提交数据列表页 - 显示所有任务及其条目完成进度"""
    db = get_db()
    month = request.args.get('month', get_current_month())
    ensure_tasks_for_month(month)
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

    task_list = []
    for t in tasks:
        items = get_items_with_status(t['task_config_id'], month)
        total_items = len(items)
        completed_items = sum(1 for i in items if i['sub_id'])
        task_dict = dict(t)
        task_dict['items'] = items
        task_dict['total_items'] = total_items
        task_dict['completed_items'] = completed_items
        task_dict['status'] = compute_task_status(t['task_config_id'], month, db)
        task_list.append(task_dict)

    return render_template('submit_list.html', tasks=task_list, month=month,
                           user_role=user['role'], user_dept=user_dept)


@app.route('/submit/task/<int:task_id>')
@require_auth
def submit_task(task_id):
    """提交任务详情页 - 显示该任务下所有条目，可逐条独立提交"""
    db = get_db()
    user = g.current_user
    month = request.args.get('month', get_current_month())
    ensure_tasks_for_month(month)

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

    # 部门隔离：director/liaison 只能访问本部门任务
    if user['role'] in ('director', 'liaison'):
        if task['dept_name'] != user.get('department', ''):
            flash('权限不足：只能访问本部门的任务', 'danger')
            return redirect(url_for('submit'))

    items = get_items_with_status(task['task_config_id'], month)

    # 获取所有模板供关联选择
    all_templates = db.execute('SELECT * FROM template_files ORDER BY name').fetchall()

    return render_template('submit_task.html', task=task, items=items, month=month, all_templates=all_templates)


@app.route('/submit/item/<int:item_id>', methods=['POST'])
@require_auth
def submit_item(item_id):
    """条目级提交 - 上传文件或标记为无"""
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
        return jsonify({'error': '条目不存在'}), 404

    # 部门隔离 + 角色校验：director/leader 不能写；liaison 仅本部门
    if not user_can_write_to_dept(g.current_user, item['dept_name']):
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
        file = request.files.get('file')
        if not file or not file.filename:
            flash('请选择要上传的文件', 'warning')
            return redirect(request.referrer or url_for('submit'))

        if not allowed_file(file.filename):
            flash(f'不支持的文件类型', 'danger')
            return redirect(request.referrer or url_for('submit'))

        stored_name, file_size, original_name, file_data = save_upload_file(file)
        # 先删除旧提交再插入
        db.execute('DELETE FROM item_submissions WHERE task_item_id = ? AND month = ?', (item_id, month))
        if USE_POSTGRES:
            db.execute('''
                INSERT INTO item_submissions (task_item_id, month, submission_type, file_name, stored_name, file_data, file_size, submitter, remarks)
                VALUES (?, ?, 'file', ?, ?, ?, ?, ?, ?)
            ''', (item_id, month, original_name, stored_name, file_data, file_size, submitter, remarks))
        else:
            db.execute('''
                INSERT INTO item_submissions (task_item_id, month, submission_type, file_name, stored_name, file_size, submitter, remarks)
                VALUES (?, ?, 'file', ?, ?, ?, ?, ?)
            ''', (item_id, month, original_name, stored_name, file_size, submitter, remarks))
        db.commit()
        flash(f'「{item["item_name"]}」文件「{original_name}」上传成功', 'success')

    # 更新任务状态
    ensure_tasks_for_month(month)

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
    ensure_tasks_for_month(month)
    flash(f'已将 {count} 个条目标记为"无"', 'success')
    return redirect(url_for('submit_task', task_id=task_id, month=month))


@app.route('/summary')
@require_auth
def summary():
    """汇总视图 - 所有条目提交情况，含模板下载列"""
    month = request.args.get('month', get_current_month())
    st_code = request.args.get('type', '')
    ensure_tasks_for_month(month)

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

    # 收集所有条目
    all_items = []
    for t in tasks:
        items = get_items_with_status(t['task_config_id'], month)
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
    return jsonify({'ok': True, 'id': new_id, 'item_name': item_name})


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
    ensure_tasks_for_month(month)
    return jsonify({'ok': True})


# ===========================================================================
# 配置管理 API
# ===========================================================================
def require_config_password(f):
    """装饰器：要求带有效密码 token（X-Config-Token 头或 form/config_token）"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get('X-Config-Token') or request.form.get('config_token')
        if not token:
            return jsonify({'error': '未授权：缺少访问令牌'}), 401
        expire = password_sessions.get(token)
        if not expire or expire < time.time():
            password_sessions.pop(token, None)
            return jsonify({'error': '会话已过期，请重新输入密码'}), 401
        # 滑动续期：每次写操作刷新过期时间
        password_sessions[token] = time.time() + PASSWORD_SESSION_TTL
        return f(*args, **kwargs)
    return wrapper


@app.route('/api/config/verify-password', methods=['POST'])
def api_verify_config_password():
    """校验配置管理密码，返回 30 分钟有效的 token"""
    db = get_db()
    password = (request.form.get('password') or '').strip()
    if not password or not password.isdigit() or len(password) != 4:
        return jsonify({'error': '请输入4位数字密码'}), 400

    row = db.execute("SELECT value FROM system_config WHERE key='config_password'").fetchone()
    stored = row['value'] if row else '1111'
    if password != stored:
        return jsonify({'error': '密码错误'}), 403

    # 生成 32 字节随机 token
    token = secrets.token_urlsafe(32)
    password_sessions[token] = time.time() + PASSWORD_SESSION_TTL
    return jsonify({
        'ok': True,
        'token': token,
        'ttl_seconds': PASSWORD_SESSION_TTL,
        'expires_at': datetime.fromtimestamp(password_sessions[token]).strftime('%Y-%m-%d %H:%M:%S'),
    })


@app.route('/api/config/change-password', methods=['POST'])
def api_change_config_password():
    """修改配置管理密码（需先通过旧密码验证）"""
    db = get_db()
    old_pwd = (request.form.get('old_password') or '').strip()
    new_pwd = (request.form.get('new_password') or '').strip()

    if not new_pwd.isdigit() or len(new_pwd) != 4:
        return jsonify({'error': '新密码必须是4位数字'}), 400

    row = db.execute("SELECT value FROM system_config WHERE key='config_password'").fetchone()
    stored = row['value'] if row else '1111'
    if old_pwd != stored:
        return jsonify({'error': '当前密码错误'}), 403

    if new_pwd == stored:
        return jsonify({'error': '新密码不能与旧密码相同'}), 400

    db.execute(
        "UPDATE system_config SET value=?, updated_at=CURRENT_TIMESTAMP WHERE key='config_password'",
        (new_pwd,)
    )
    db.commit()
    # 让所有现有 token 失效，下次需重新输入新密码
    password_sessions.clear()
    return jsonify({'ok': True, 'message': '密码已修改，请重新登录'})


@app.route('/api/config/check-session', methods=['GET'])
def api_check_config_session():
    """检查 token 是否有效（前端定期检查）"""
    token = request.headers.get('X-Config-Token')
    if not token:
        return jsonify({'ok': False}), 200
    expire = password_sessions.get(token)
    if not expire or expire < time.time():
        password_sessions.pop(token, None)
        return jsonify({'ok': False}), 200
    remaining = int(expire - time.time())
    return jsonify({'ok': True, 'remaining_seconds': remaining})


@app.route('/api/config/logout', methods=['POST'])
def api_config_logout():
    """主动登出：清掉 token"""
    token = request.headers.get('X-Config-Token') or request.form.get('config_token')
    if token:
        password_sessions.pop(token, None)
    return jsonify({'ok': True})


@app.route('/api/config/update-deadline', methods=['POST'])
@require_config_password
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
@require_config_password
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


@app.route('/download/template/<int:tid>')
def download_template_by_id(tid):
    """下载模板文件"""
    db = get_db()
    tpl = db.execute('SELECT * FROM template_files WHERE id = ?', (tid,)).fetchone()
    if not tpl:
        abort(404)
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
    file = request.files.get('file')

    if not file or not file.filename:
        flash('请选择文件', 'warning')
        return redirect(url_for('templates_page'))

    if not allowed_file(file.filename):
        flash('不支持的文件类型', 'danger')
        return redirect(url_for('templates_page'))

    if not name:
        name = os.path.splitext(file.filename)[0]

    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    original = secure_filename(file.filename)
    if not original:
        original = 'template_file'
    stored_name = f"{timestamp}_{original}"

    st_names = {
        'upstream': '对上结算',
        'downstream': '对下结算',
        'common': '通用'
    }

    if USE_POSTGRES:
        file_data = file.read()
        file_size = len(file_data)
        db.execute('''
            INSERT INTO template_files (name, description, settlement_type, department, file_name, stored_name, file_data, file_size, uploader)
            VALUES (?,?,?,?,?,?,?,?,?)
        ''', (name, description, st_names.get(settlement_type, '通用'), department,
              original, stored_name, file_data, file_size, uploader))
    else:
        filepath = os.path.join(TEMPLATE_DIR, stored_name)
        file.save(filepath)
        file_size = os.path.getsize(filepath)
        db.execute('''
            INSERT INTO template_files (name, description, settlement_type, department, file_name, stored_name, file_size, uploader)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (name, description, st_names.get(settlement_type, '通用'), department,
              original, stored_name, file_size, uploader))
    db.commit()
    flash(f'模板文件「{name}」上传成功', 'success')
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
@require_config_password
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
    ensure_tasks_for_month(month)
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
        status = compute_task_status(t['task_config_id'], month, db)
        if status == 'completed':
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
#   liaison   - 各部门联络人（上传/查阅/删除本部门文件，密码暂未确定）
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
    {'username': 'director_rlzyb', 'display_name': '人力资源部主任', 'password': 'rlzyb555', 'role': 'director', 'department': '人力资源部', 'phone': ''},
    {'username': 'director_zhbgs', 'display_name': '综合办公室主任', 'password': 'zhbgs666', 'role': 'director', 'department': '综合办公室', 'phone': ''},
    # 4. 各部门联络人（密码暂未确定，用临时密码占位，管理员可在用户管理中改）
    {'username': 'liaison_scdd', 'display_name': '生产调度部联络人', 'password': 'scddb_liaison_待定', 'role': 'liaison', 'department': '生产调度部', 'phone': ''},
    {'username': 'liaison_gczljs', 'display_name': '工程质量技术部联络人', 'password': 'gczljsb_liaison_待定', 'role': 'liaison', 'department': '工程质量技术部', 'phone': ''},
    {'username': 'liaison_aqhbb', 'display_name': '安全环保部联络人', 'password': 'aqhbb_liaison_待定', 'role': 'liaison', 'department': '安全环保部', 'phone': ''},
    {'username': 'liaison_sbwzb', 'display_name': '设备物资部联络人', 'password': 'sbwzb_liaison_待定', 'role': 'liaison', 'department': '设备物资部', 'phone': ''},
    {'username': 'liaison_rlzyb', 'display_name': '人力资源部联络人', 'password': 'rlzyb_liaison_待定', 'role': 'liaison', 'department': '人力资源部', 'phone': ''},
    {'username': 'liaison_zhbgs', 'display_name': '综合办公室联络人', 'password': 'zhbgs_liaison_待定', 'role': 'liaison', 'department': '综合办公室', 'phone': ''},
]

# 已注册部门列表（用于前端下拉）
DEPARTMENT_LIST = [
    '生产调度部', '工程质量技术部', '安全环保部', '设备物资部',
    '人力资源部', '综合办公室', '合同管理部',
    '项目领导',
]

# 用户会话：token -> 用户信息 + 过期时间
USER_SESSION_TTL = 8 * 3600  # 8 小时
user_sessions = {}  # {token: {'user': dict, 'expire': ts}}


def generate_token():
    """生成 32 字节 URL-safe 随机 token"""
    return secrets.token_urlsafe(32)


def parse_token(token):
    """解析 token，返回用户信息或 None"""
    if not token:
        return None
    info = user_sessions.get(token)
    if not info:
        return None
    if info['expire'] < time.time():
        user_sessions.pop(token, None)
        return None
    # 滑动续期
    info['expire'] = time.time() + USER_SESSION_TTL
    return info['user']


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
    return render_template('login.html', departments=DEPARTMENT_LIST)


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
    user_sessions[token] = {
        'user': user_dict,
        'expire': time.time() + USER_SESSION_TTL
    }

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
        user_sessions.pop(token, None)
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
        user_sessions.pop(token, None)
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

    query = 'SELECT id, direction, project_name, counterparty, contract_no, amount, currency, settle_date, status, notes, attachment_name, attachment_size, created_by, created_at, updated_at FROM settlement_records WHERE 1=1'
    params = []
    if direction in ('up', 'down'):
        query += ' AND direction = ?'
        params.append(direction)
    if status:
        query += ' AND status = ?'
        params.append(status)
    if project:
        query += ' AND project_name LIKE ?'
        params.append(f'%{project}%')
    if department_filter:
        query += ' AND (created_by LIKE ? OR notes LIKE ?)'
        params.extend([f'%{department_filter}%', f'%{department_filter}%'])

    # 访问角色：仅 leader + admin（按 @require_role 拦截）
    # 此处不再追加 director/liaison 部门过滤

    query += ' ORDER BY settle_date DESC, id DESC'
    rows = db.execute(query, params).fetchall()

    records = []
    for r in rows:
        d = dict(r)
        d['amount'] = float(d['amount']) if d['amount'] is not None else 0.0
        records.append(d)
    return jsonify({'ok': True, 'records': records})


@app.route('/api/settlement-records/summary')
@require_role('leader', 'admin')
def api_settlement_summary():
    """汇总统计卡（对上/对下 总金额、已完成、办理中、本月新增）
    访问角色：仅 leader + admin（按 @require_role 拦截），不再追加部门过滤。
    """
    db = get_db()

    def safe_query(sql, params):
        try:
            return db.execute(sql, params).fetchone()
        except Exception:
            return None

    def get_one(direction):
        rows = safe_query(
            "SELECT COALESCE(SUM(amount), 0) as total, COUNT(*) as cnt FROM settlement_records WHERE direction = ?",
            [direction]
        )
        return float(rows['total']) if rows else 0.0, (rows['cnt'] if rows else 0)

    def get_status_sum(direction, status):
        rows = safe_query(
            "SELECT COALESCE(SUM(amount), 0) as total FROM settlement_records WHERE direction = ? AND status = ?",
            [direction, status]
        )
        return float(rows['total']) if rows else 0.0

    def get_month_new(direction):
        month_prefix = datetime.now().strftime('%Y-%m')
        rows = safe_query(
            "SELECT COALESCE(SUM(amount), 0) as total, COUNT(*) as cnt FROM settlement_records WHERE direction = ? AND settle_date LIKE ?",
            [direction, f'{month_prefix}%']
        )
        return float(rows['total']) if rows else 0.0, (rows['cnt'] if rows else 0)

    up_total, up_cnt = get_one('up')
    down_total, down_cnt = get_one('down')
    up_completed = get_status_sum('up', 'completed')
    down_completed = get_status_sum('down', 'completed')
    up_pending = get_status_sum('up', 'pending') + get_status_sum('up', 'processing')
    down_pending = get_status_sum('down', 'pending') + get_status_sum('down', 'processing')
    up_month_amt, up_month_cnt = get_month_new('up')
    down_month_amt, down_month_cnt = get_month_new('down')

    return jsonify({
        'ok': True,
        'upstream': {
            'total_amount': up_total,
            'total_count': up_cnt,
            'completed_amount': up_completed,
            'pending_amount': up_pending,
            'month_amount': up_month_amt,
            'month_count': up_month_cnt,
        },
        'downstream': {
            'total_amount': down_total,
            'total_count': down_cnt,
            'completed_amount': down_completed,
            'pending_amount': down_pending,
            'month_amount': down_month_amt,
            'month_count': down_month_cnt,
        },
    })


@app.route('/api/settlement-records', methods=['POST'])
@require_role('admin')
def api_create_settlement_record():
    """新建结算金额记录（仅管理员）"""
    db = get_db()
    user = g.current_user

    direction = (request.form.get('direction') or '').strip()
    project_name = (request.form.get('project_name') or '').strip()
    counterparty = (request.form.get('counterparty') or '').strip()
    contract_no = (request.form.get('contract_no') or '').strip()
    amount_raw = (request.form.get('amount') or '0').strip()
    currency = (request.form.get('currency') or 'PHP').strip()
    settle_date = (request.form.get('settle_date') or '').strip()
    status = (request.form.get('status') or 'pending').strip()
    notes = (request.form.get('notes') or '').strip()
    department_tag = (request.form.get('department') or user.get('department', '')).strip()

    if direction not in ('up', 'down'):
        return jsonify({'error': '结算方向必须为 up 或 down'}), 400
    if not project_name:
        return jsonify({'error': '请填写项目名称'}), 400
    if not counterparty:
        return jsonify({'error': '请填写对方单位'}), 400
    try:
        amount = float(amount_raw)
    except ValueError:
        return jsonify({'error': '金额必须是数字'}), 400
    if amount < 0:
        return jsonify({'error': '金额不能为负数'}), 400

    attachment_name = ''
    attachment_stored = ''
    attachment_data = None
    attachment_size = 0

    file = request.files.get('attachment')
    if file and file.filename:
        if not allowed_file(file.filename):
            return jsonify({'error': '不支持的附件类型'}), 400
        stored, size, original, data = save_upload_file(file, subdir='settlement_attachments')
        attachment_name = original
        attachment_stored = stored
        attachment_size = size
        attachment_data = data

    created_by = f"{user['display_name']}({user['role']})"
    # 把部门信息写入 notes，便于按部门过滤（director/liaison 的过滤查询依赖此字段）
    if department_tag and department_tag not in notes:
        notes = f"[{department_tag}] {notes}".strip()

    if USE_POSTGRES:
        new_id = insert_returning_id(db, '''
            INSERT INTO settlement_records
            (direction, project_name, counterparty, contract_no, amount, currency, settle_date, status, notes,
             attachment_name, attachment_stored, attachment_data, attachment_size, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (direction, project_name, counterparty, contract_no, amount, currency,
              settle_date or None, status, notes,
              attachment_name, attachment_stored, attachment_data, attachment_size, created_by))
    else:
        db.execute('''
            INSERT INTO settlement_records
            (direction, project_name, counterparty, contract_no, amount, currency, settle_date, status, notes,
             attachment_name, attachment_stored, attachment_size, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (direction, project_name, counterparty, contract_no, amount, currency,
              settle_date or None, status, notes,
              attachment_name, attachment_stored, attachment_size, created_by))
        new_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
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

    direction = (request.form.get('direction') or row['direction']).strip()
    project_name = (request.form.get('project_name') or row['project_name']).strip()
    counterparty = (request.form.get('counterparty') or row['counterparty']).strip()
    contract_no = (request.form.get('contract_no') or row['contract_no'] or '').strip()
    amount_raw = request.form.get('amount', str(row['amount'] or 0))
    currency = (request.form.get('currency') or row['currency'] or 'PHP').strip()
    settle_date = (request.form.get('settle_date') or row['settle_date'] or '').strip()
    status = (request.form.get('status') or row['status'] or 'pending').strip()
    notes = (request.form.get('notes') or row['notes'] or '').strip()

    try:
        amount = float(amount_raw)
    except ValueError:
        return jsonify({'error': '金额必须是数字'}), 400

    file = request.files.get('attachment')
    attachment_name = row['attachment_name']
    attachment_stored = row['attachment_stored']
    attachment_data = row['attachment_data']
    attachment_size = row['attachment_size'] or 0
    if file and file.filename:
        if not allowed_file(file.filename):
            return jsonify({'error': '不支持的附件类型'}), 400
        stored, size, original, data = save_upload_file(file, subdir='settlement_attachments')
        attachment_name = original
        attachment_stored = stored
        attachment_size = size
        attachment_data = data

    if USE_POSTGRES:
        db.execute('''
            UPDATE settlement_records
            SET direction=?, project_name=?, counterparty=?, contract_no=?, amount=?, currency=?, settle_date=?,
                status=?, notes=?, attachment_name=?, attachment_stored=?, attachment_data=?, attachment_size=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (direction, project_name, counterparty, contract_no, amount, currency,
              settle_date or None, status, notes,
              attachment_name, attachment_stored, attachment_data, attachment_size, rid))
    else:
        db.execute('''
            UPDATE settlement_records
            SET direction=?, project_name=?, counterparty=?, contract_no=?, amount=?, currency=?, settle_date=?,
                status=?, notes=?, attachment_name=?, attachment_stored=?, attachment_size=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (direction, project_name, counterparty, contract_no, amount, currency,
              settle_date or None, status, notes,
              attachment_name, attachment_stored, attachment_size, rid))
    db.commit()
    return jsonify({'ok': True})


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


@app.route('/download/settlement-attachment/<int:rid>')
@require_role('leader', 'admin')
def download_settlement_attachment(rid):
    """下载结算金额附件"""
    db = get_db()
    row = db.execute('SELECT * FROM settlement_records WHERE id = ?', (rid,)).fetchone()
    if not row or not row['attachment_stored']:
        abort(404)
    if USE_POSTGRES:
        if not row['attachment_data']:
            abort(404)
        return send_file(io.BytesIO(row['attachment_data']), as_attachment=True, download_name=row['attachment_name'])
    filepath = os.path.join(UPLOAD_DIR, 'settlement_attachments', row['attachment_stored'])
    if not os.path.exists(filepath):
        abort(404)
    return send_file(filepath, as_attachment=True, download_name=row['attachment_name'])


# ---------- 部门文件管理 ----------
# 说明：用户要求"不用再加部门文件模块"，此模块已下线。
# 为防止通过旧链接/书签直链误入，本路由直接 404；如需恢复可放开 @require_auth。
@app.route('/department-files')
def department_files_page():
    """部门文件管理页 - 已下线（统一返回 404）"""
    from flask import abort
    abort(404)


@app.route('/api/department-files', methods=['GET'])
@require_auth
def api_list_department_files():
    """查询部门文件列表"""
    db = get_db()
    user = g.current_user

    department_filter = request.args.get('department', '').strip()

    query = 'SELECT id, department, file_name, stored_name, file_size, uploader, uploader_department, description, uploaded_at FROM department_files WHERE 1=1'
    params = []
    if department_filter:
        query += ' AND department = ?'
        params.append(department_filter)
    # 角色过滤
    if user['role'] in ('director', 'liaison'):
        dept = user.get('department', '')
        if dept:
            query += ' AND department = ?'
            params.append(dept)
    query += ' ORDER BY uploaded_at DESC'
    rows = db.execute(query, params).fetchall()
    files = []
    for r in rows:
        d = dict(r)
        d['file_size'] = int(d['file_size']) if d['file_size'] is not None else 0
        files.append(d)
    return jsonify({'ok': True, 'files': files})


@app.route('/api/department-files', methods=['POST'])
@require_role('admin', 'liaison')
def api_upload_department_file():
    """上传部门文件（仅管理员 + 联络人）

    - admin：可上传到任意部门
    - liaison：只能上传到本部门（系统自动锁定）
    """
    db = get_db()
    user = g.current_user

    department = (request.form.get('department') or user.get('department', '')).strip()
    description = (request.form.get('description') or '').strip()
    file = request.files.get('file')

    if not file or not file.filename:
        return jsonify({'error': '请选择文件'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': '不支持的文件类型'}), 400
    if not department:
        return jsonify({'error': '请指定部门'}), 400

    # 联络人：强制限只能上传到自己的部门（防止选错）
    if user['role'] == 'liaison':
        own_dept = user.get('department', '')
        if department != own_dept:
            return jsonify({'error': f'联络人只能上传到本部门（{own_dept}）'}), 403

    stored, size, original, data = save_upload_file(file, subdir='department_files')

    uploader = user['display_name']
    uploader_dept = user.get('department', '')

    if USE_POSTGRES:
        new_id = insert_returning_id(db, '''
            INSERT INTO department_files
            (department, file_name, stored_name, file_data, file_size, uploader, uploader_department, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (department, original, stored, data, size, uploader, uploader_dept, description))
    else:
        db.execute('''
            INSERT INTO department_files
            (department, file_name, stored_name, file_size, uploader, uploader_department, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (department, original, stored, size, uploader, uploader_dept, description))
        new_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.commit()
    return jsonify({'ok': True, 'id': new_id})


@app.route('/api/department-files/<int:fid>', methods=['DELETE'])
@require_role('admin', 'liaison')
def api_delete_department_file(fid):
    """删除部门文件

    - admin：可删任意
    - liaison：只能删自己上传的文件（且必须属于本部门）
    - director：没有删除权限（只读）
    """
    db = get_db()
    user = g.current_user

    row = db.execute('SELECT * FROM department_files WHERE id = ?', (fid,)).fetchone()
    if not row:
        return jsonify({'error': '文件不存在'}), 404
    if user['role'] == 'liaison':
        own_dept = user.get('department', '')
        if row['department'] != own_dept:
            return jsonify({'error': '只能删除本部门文件'}), 403
        if row['uploader'] != user['display_name']:
            return jsonify({'error': '联络人只能删除自己上传的文件'}), 403

    db.execute('DELETE FROM department_files WHERE id = ?', (fid,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/download/department-file/<int:fid>')
@require_auth
def download_department_file(fid):
    """下载部门文件"""
    db = get_db()
    user = g.current_user
    row = db.execute('SELECT * FROM department_files WHERE id = ?', (fid,)).fetchone()
    if not row:
        abort(404)
    # 角色权限校验
    if user['role'] in ('director', 'liaison'):
        if row['department'] != user.get('department', ''):
            abort(403)
    if USE_POSTGRES:
        if not row['file_data']:
            abort(404)
        return send_file(io.BytesIO(row['file_data']), as_attachment=True, download_name=row['file_name'])
    filepath = os.path.join(UPLOAD_DIR, 'department_files', row['stored_name'])
    if not os.path.exists(filepath):
        abort(404)
    return send_file(filepath, as_attachment=True, download_name=row['file_name'])


@app.route('/api/_init-users', methods=['GET'])
def api_init_users():
    """首次访问自动 seed 用户（公开接口，仅当 users 表为空时生效）

    支持表不存在的情况：先用全新连接跑 init_schema 建表 + commit，
    再用同一个全新连接 SELECT 验证表已存在，再 seed。
    """
    fresh = connect()
    try:
        # 1. 验证/创建 users 表
        try:
            count = fresh.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
            print(f'[_init-users] users 表已存在，行数 {count}')
        except Exception as e:
            print(f'[_init-users] users 表不存在，开始建表：{e}')
            fresh.rollback()  # 关键：清掉 SELECT 失败造成的 aborted 状态
            init_schema(fresh)
            fresh.commit()
            # 立刻用同一个连接 SELECT 验证
            count = fresh.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
            print(f'[_init-users] 同一连接 SELECT 验证：行数 {count}')
        # 2. seed（仍在同一连接，避免 Neon PgBouncer 跨连接问题）
        if count == 0:
            seed_users(fresh)
            fresh.commit()
            return jsonify({'ok': True, 'seeded': True, 'message': '已初始化 10 个内置用户'})
        return jsonify({'ok': True, 'seeded': False, 'message': f'已存在 {count} 个用户'})
    except Exception as e:
        print(f'[_init-users] 失败：{e}')
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        fresh.close()


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
    }


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


# 2026-08-05: 清理完一次性端点后，仪表盘不再显示"全部部门对比·排名榜"

