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
# Vercel Hobby 限制 4.5MB，本地无限制
if USE_POSTGRES:
    app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024  # 4MB (Vercel safe)
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
@app.route('/')
def dashboard():
    month = request.args.get('month', get_current_month())
    ensure_tasks_for_month(month)

    db = get_db()
    tasks = db.execute('''
        SELECT t.*, d.name as dept_name, d.contact_person, d.sort_order,
               st.code as st_code, st.name as st_name,
               tc.id as task_config_id, tc.remarks as config_remarks
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

    return render_template('dashboard.html',
                           month=month, tasks=task_list,
                           total=total, completed=completed,
                           pending=pending, partial=partial, overdue=overdue,
                           upstream_tasks=upstream_tasks,
                           downstream_tasks=downstream_tasks)


@app.route('/submit', methods=['GET'])
def submit():
    """提交数据列表页 - 显示所有任务及其条目完成进度"""
    db = get_db()
    month = request.args.get('month', get_current_month())
    ensure_tasks_for_month(month)

    tasks = db.execute('''
        SELECT t.*, d.name as dept_name, d.contact_person, d.sort_order,
               st.code as st_code, st.name as st_name,
               tc.id as task_config_id, tc.deadline_day, tc.remarks as config_remarks
        FROM tasks t
        JOIN departments d ON t.department_id = d.id
        JOIN settlement_types st ON t.settlement_type_id = st.id
        JOIN task_configs tc ON t.task_config_id = tc.id
        WHERE t.month = ?
        ORDER BY st.id, d.sort_order
    ''', (month,)).fetchall()

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

    return render_template('submit_list.html', tasks=task_list, month=month)


@app.route('/submit/task/<int:task_id>')
def submit_task(task_id):
    """提交任务详情页 - 显示该任务下所有条目，可逐条独立提交"""
    db = get_db()
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

    items = get_items_with_status(task['task_config_id'], month)

    # 获取所有模板供关联选择
    all_templates = db.execute('SELECT * FROM template_files ORDER BY name').fetchall()

    return render_template('submit_task.html', task=task, items=items, month=month, all_templates=all_templates)


@app.route('/submit/item/<int:item_id>', methods=['POST'])
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
def mark_all_none(task_id):
    """将某任务下所有未提交条目标记为无"""
    db = get_db()
    month = request.form.get('month', get_current_month())
    submitter = request.form.get('submitter', '管理员').strip()

    task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not task:
        abort(404)

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
def summary():
    """汇总视图 - 所有条目提交情况，含模板下载列"""
    month = request.args.get('month', get_current_month())
    st_code = request.args.get('type', '')
    ensure_tasks_for_month(month)

    db = get_db()

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
def api_edit_item():
    """编辑条目名称"""
    db = get_db()
    item_id = request.form.get('item_id')
    item_name = request.form.get('item_name', '').strip()
    description = request.form.get('description', '').strip()

    if not item_name:
        return jsonify({'error': '条目名称不能为空'}), 400

    item = db.execute('SELECT * FROM task_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '条目不存在'}), 404

    db.execute('UPDATE task_items SET item_name = ?, description = ? WHERE id = ?',
               (item_name, description, item_id))
    db.commit()
    return jsonify({'ok': True, 'item_name': item_name, 'description': description})


@app.route('/api/item/add', methods=['POST'])
def api_add_item():
    """新增条目"""
    db = get_db()
    task_config_id = request.form.get('task_config_id')
    item_name = request.form.get('item_name', '').strip()
    template_file_id = request.form.get('template_file_id') or None

    if not item_name:
        return jsonify({'error': '条目名称不能为空'}), 400

    max_order = db.execute('SELECT MAX(sort_order) as m FROM task_items WHERE task_config_id = ?', (task_config_id,)).fetchone()['m'] or 0
    db.execute('''
        INSERT INTO task_items (task_config_id, item_name, template_file_id, sort_order, is_active)
        VALUES (?,?,?,?,1)
    ''', (task_config_id, item_name, template_file_id, max_order + 1))
    db.commit()
    new_id = db.execute('SELECT MAX(id) as m FROM task_items WHERE task_config_id=?', (task_config_id,)).fetchone()['m']
    return jsonify({'ok': True, 'id': new_id, 'item_name': item_name})


@app.route('/api/item/delete', methods=['POST'])
def api_delete_item():
    """删除条目"""
    db = get_db()
    item_id = request.form.get('item_id')
    item = db.execute('SELECT * FROM task_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '条目不存在'}), 404

    db.execute('DELETE FROM item_submissions WHERE task_item_id = ?', (item_id,))
    db.execute('DELETE FROM task_items WHERE id = ?', (item_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/item/link-template', methods=['POST'])
def api_link_template():
    """关联条目与模板文件"""
    db = get_db()
    item_id = request.form.get('item_id')
    template_file_id = request.form.get('template_file_id') or None

    item = db.execute('SELECT * FROM task_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'error': '条目不存在'}), 404

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
def api_unsubmit_item():
    """撤销条目提交"""
    db = get_db()
    item_id = request.form.get('item_id')
    month = request.form.get('month', get_current_month())

    sub = db.execute('SELECT * FROM item_submissions WHERE task_item_id = ? AND month = ?', (item_id, month)).fetchone()
    if not sub:
        return jsonify({'error': '无提交记录'}), 404

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
@app.route('/api/config/update-deadline', methods=['POST'])
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
