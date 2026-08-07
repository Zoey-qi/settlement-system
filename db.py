# -*- coding: utf-8 -*-
"""
数据库适配层
本地：SQLite  |  Vercel：PostgreSQL
同一套代码，自动切换
"""
import os
import re

# 检测是否在 Vercel 环境（有 PostgreSQL 连接串）
DATABASE_URL = os.environ.get('POSTGRES_URL') or os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL_NON_POOLING')
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import DictCursor


class EmptyCursor:
    """空游标，用于 no-op 操作"""
    def fetchone(self): return None
    def fetchall(self): return []
    def fetchmany(self, n): return []


class PgConnection:
    """psycopg2 包装器，提供与 sqlite3.Connection 兼容的接口"""

    def __init__(self, dsn):
        # connect_timeout：连接失败快速返回，避免冷/异常连接在握手阶段长时间挂起
        self._conn = psycopg2.connect(dsn, connect_timeout=10)
        # 使用 autocommit 模式：避免 PgBouncer transaction pool 下 DDL/commit 跨连接不可见
        self._conn.autocommit = True

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        pass  # DictCursor 已处理

    def execute(self, sql, params=None):
        sql = self._adapt_sql(sql)
        if params is None:
            params = []
        elif not isinstance(params, (list, tuple)):
            params = [params]

        cur = self._conn.cursor(cursor_factory=DictCursor)
        cur.execute(sql, params)
        return cur

    def commit(self):
        # autocommit=True 下 commit 是 no-op，保留兼容
        try:
            self._conn.commit()
        except Exception:
            pass

    def rollback(self):
        # autocommit=True 下 rollback 是 no-op
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        # serverless 下不真正关闭：该连接由模块级缓存复用（见 _get_pg_conn），
        # 随 lambda 实例销毁而释放。若在此关闭，teardown 会杀掉暖实例可复用的连接，反而更慢。
        pass

    def _adapt_sql(self, sql):
        """将 SQLite SQL 转换为 PostgreSQL SQL"""
        # ? → %s
        sql = sql.replace('?', '%s')

        # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
        if 'INSERT OR IGNORE' in sql.upper():
            sql = re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql, flags=re.IGNORECASE)
            if 'ON CONFLICT' not in sql.upper():
                sql = sql.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'

        # last_insert_rowid() → lastval()
        sql = sql.replace('last_insert_rowid()', 'lastval()')

        # PRAGMA → 跳过（返回空）
        if sql.strip().upper().startswith('PRAGMA'):
            return 'SELECT 1 WHERE 1=0'  # no-op

        # datetime('now') → NOW()
        sql = sql.replace("datetime('now')", 'NOW()')

        return sql


# 模块级连接缓存：Vercel 暖实例（warm start）可跨请求复用同一条连接，
# 避免每次请求都做 TCP+TLS 握手——这是 serverless 下最稳定、占比最高的延迟来源（约 100~300ms/次）。
_PG_CONN = None


def _pg_is_alive(conn):
    """轻量探活；连接断开或被 Neon 回收（空闲超时）时返回 False"""
    try:
        with conn._conn.cursor() as cur:
            cur.execute('SELECT 1')
        return True
    except Exception:
        return False


def _get_pg_conn():
    """返回可复用的 PostgreSQL 连接；失效则自动重建（自愈）"""
    global _PG_CONN
    if _PG_CONN is not None and _pg_is_alive(_PG_CONN):
        return _PG_CONN
    _PG_CONN = PgConnection(DATABASE_URL)
    return _PG_CONN


def connect():
    """获取数据库连接"""
    if USE_POSTGRES:
        return _get_pg_conn()
    else:
        import sqlite3
        # Vercel 文件系统只读，用 /tmp 作为回退
        if os.environ.get('VERCEL'):
            db_dir = '/tmp'
        else:
            db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
        os.makedirs(db_dir, exist_ok=True)
        DB_PATH = os.path.join(db_dir, 'settlement.db')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        return conn


def insert_returning_id(db, sql, params=None):
    """执行 INSERT 并返回新行 ID（兼容 SQLite 和 PostgreSQL）"""
    if USE_POSTGRES:
        adapted_sql = db._adapt_sql(sql)
        if 'RETURNING' not in adapted_sql.upper():
            adapted_sql = adapted_sql.rstrip().rstrip(';') + ' RETURNING id'
        cur = db._conn.cursor(cursor_factory=DictCursor)
        cur.execute(adapted_sql, params or [])
        return cur.fetchone()[0]
    else:
        db.execute(sql, params or [])
        return db.execute('SELECT last_insert_rowid()').fetchone()[0]


def get_auto_inc():
    """返回自增主键语法"""
    if USE_POSTGRES:
        return 'SERIAL PRIMARY KEY'
    return 'INTEGER PRIMARY KEY AUTOINCREMENT'


def init_schema(db):
    """创建数据库表（兼容两种数据库）"""
    ai = get_auto_inc()

    tables = [
        f'''CREATE TABLE IF NOT EXISTS departments (
            id {ai},
            name TEXT NOT NULL UNIQUE,
            contact_person TEXT,
            sort_order INTEGER DEFAULT 0
        )''',
        f'''CREATE TABLE IF NOT EXISTS settlement_types (
            id {ai},
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL
        )''',
        f'''CREATE TABLE IF NOT EXISTS task_configs (
            id {ai},
            department_id INTEGER NOT NULL,
            settlement_type_id INTEGER NOT NULL,
            required_materials TEXT NOT NULL,
            deadline_day INTEGER NOT NULL,
            remarks TEXT,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (department_id) REFERENCES departments(id),
            FOREIGN KEY (settlement_type_id) REFERENCES settlement_types(id),
            UNIQUE(department_id, settlement_type_id)
        )''',
        f'''CREATE TABLE IF NOT EXISTS tasks (
            id {ai},
            task_config_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            department_id INTEGER NOT NULL,
            settlement_type_id INTEGER NOT NULL,
            required_materials TEXT NOT NULL,
            deadline_day INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_config_id) REFERENCES task_configs(id),
            FOREIGN KEY (department_id) REFERENCES departments(id),
            FOREIGN KEY (settlement_type_id) REFERENCES settlement_types(id),
            UNIQUE(task_config_id, month)
        )''',
        f'''CREATE TABLE IF NOT EXISTS submissions (
            id {ai},
            task_id INTEGER NOT NULL,
            submission_type TEXT NOT NULL,
            file_name TEXT,
            stored_name TEXT,
            file_size INTEGER,
            submitter TEXT,
            remarks TEXT,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )''',
        f'''CREATE TABLE IF NOT EXISTS template_files (
            id {ai},
            name TEXT NOT NULL,
            description TEXT,
            settlement_type TEXT,
            department TEXT,
            file_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            file_data BYTEA,
            file_size INTEGER,
            uploader TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        f'''CREATE TABLE IF NOT EXISTS config_files (
            id {ai},
            file_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            departments_count INTEGER DEFAULT 0,
            tasks_count INTEGER DEFAULT 0,
            uploader TEXT
        )''',
        f'''CREATE TABLE IF NOT EXISTS task_items (
            id {ai},
            task_config_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            description TEXT,
            template_file_id INTEGER,
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (task_config_id) REFERENCES task_configs(id),
            FOREIGN KEY (template_file_id) REFERENCES template_files(id)
        )''',
        f'''CREATE TABLE IF NOT EXISTS item_submissions (
            id {ai},
            task_item_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            submission_type TEXT NOT NULL,
            file_name TEXT,
            stored_name TEXT,
            file_data BYTEA,
            file_size INTEGER,
            submitter TEXT,
            remarks TEXT,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_item_id, month),
            FOREIGN KEY (task_item_id) REFERENCES task_items(id)
        )''',
        f'''CREATE TABLE IF NOT EXISTS fee_rates (
            id {ai},
            fee_type TEXT NOT NULL UNIQUE,
            fee_name TEXT NOT NULL,
            rate_value TEXT,
            rate_unit TEXT,
            calc_method TEXT,
            note TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        f'''CREATE TABLE IF NOT EXISTS guide_content (
            id INTEGER PRIMARY KEY DEFAULT 1,
            content TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        f'''CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        # =================================================================
        # 新增：登录权限系统 + 对上对下结算金额统计 + 部门文件管理
        # =================================================================
        f'''CREATE TABLE IF NOT EXISTS users (
            id {ai},
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            department TEXT,
            phone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        f'''CREATE TABLE IF NOT EXISTS settlement_records (
            id {ai},
            direction TEXT NOT NULL,
            project_name TEXT NOT NULL,
            counterparty TEXT NOT NULL,
            contract_no TEXT,
            amount NUMERIC DEFAULT 0,
            currency TEXT DEFAULT 'PHP',
            settle_date DATE,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            attachment_name TEXT,
            attachment_stored TEXT,
            attachment_data BYTEA,
            attachment_size INTEGER,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        f'''CREATE TABLE IF NOT EXISTS department_files (
            id {ai},
            department TEXT NOT NULL,
            file_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            file_data BYTEA,
            file_size INTEGER,
            uploader TEXT,
            uploader_department TEXT,
            description TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        # 用户登录会话表（持久化，跨 Serverless 冷启动）
        # Vercel Serverless 每次冷启动会清空内存字典，故 session 必须落库
        f'''CREATE TABLE IF NOT EXISTS user_sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL,
            department TEXT,
            phone TEXT,
            expire_ts BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
    ]

    for i, table_sql in enumerate(tables):
        try:
            db.execute(table_sql)
        except Exception as e:
            # 单条 CREATE 失败不影响后续（兼容历史库已有部分表的情况）
            print(f'[init_schema] skip table (already exists or incompatible): {e}')

    # 兼容已有数据库：为历史 departments 表补充联系人字段
    try:
        db.execute('ALTER TABLE departments ADD COLUMN contact_person TEXT')
    except Exception:
        pass
    db.commit()


def seed_default_data(db):
    """填充默认数据（部门、结算类型、费率、任务配置）"""
    from datetime import datetime

    # 收费标准
    default_rates = [
        ('accommodation', '住宿费', '2000', '比索/间', '房间数 x 单价', '每间房间固定2000比索，每间住的人数不固定'),
        ('meal', '餐费（伙食费）', '30', '比索/份', '份数 x 单价', '每份30比索'),
        ('water', '生活用水（水费）', '待确认', '', '待确认', '需单独联系总包确认收费标准'),
        ('electricity', '生活用电（电费）', '待确认', '', '待确认', '需单独联系总包确认收费标准'),
    ]
    for fee_type, fee_name, rate_value, rate_unit, calc_method, note in default_rates:
        db.execute('''
            INSERT INTO fee_rates (fee_type, fee_name, rate_value, rate_unit, calc_method, note)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT DO NOTHING
        ''', (fee_type, fee_name, rate_value, rate_unit, calc_method, note))

    # 结算类型
    db.execute("INSERT INTO settlement_types (code, name) VALUES ('upstream', '对上结算（总包）') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO settlement_types (code, name) VALUES ('downstream', '对下结算（分包）') ON CONFLICT DO NOTHING")

    # 部门
    departments = [
        ('生产调度部', '', 1), ('工程质量技术部', '', 2),
        ('安全环保部', '', 3), ('设备物资部', '', 4),
        ('人力资源部', '', 5), ('综合办公室', '', 6),
        ('财务资金部', '', 7), ('合同管理部', '', 8),
    ]
    for name, person, order in departments:
        db.execute(
            "INSERT INTO departments (name, contact_person, sort_order) VALUES (?,?,?) ON CONFLICT DO NOTHING",
            (name, person, order)
        )

    # 任务配置
    up = db.execute("SELECT id FROM settlement_types WHERE code='upstream'").fetchone()[0]
    down = db.execute("SELECT id FROM settlement_types WHERE code='downstream'").fetchone()[0]

    upstream = [
        ('生产调度部', up, '当月完成工程量报表、施工日志汇总、借用设备确认单（与物资部确认是否有使用联营体零星机械）', 28, '结算基础依据'),
        ('工程质量技术部', up, '质量验收记录、监理验工资料、对应图纸、专项计量资料、图纸工程量对比表、计量计算底稿等', 29, '不合格不予结算'),
        ('安全环保部', up, '总包安全检查整改回执、HSE罚款', 29, '涉及安全费扣款/计取'),
        ('设备物资部', up, '分包商材料领用表（甲供材消耗统计表，如柴油、联营体物资采购部张伊凡）、安全领用材料、设备使用扣款单、材料调拨单', 30, '扣回甲供材费用'),
        ('人力资源部', up, '农民工工资发放表、考勤表', 30, '证明无欠薪'),
        ('综合办公室', up, '分包人员宿舍租赁、水电、食堂伙食费扣款单（联系综合工区张勇或其他联营体指定人员）', 30, '总承包管理费分摊'),
        ('财务资金部', up, '已收款台账、发票开具情况、税金预缴凭证', 30, '核对累计结算金额'),
    ]
    downstream = [
        ('生产调度部', down, '分包当月完成工程量核定表，借用设备确认单（与物资部确认是否有使用联营体零星机械）', 28, '核量控制（以生产部为准），防止虚报'),
        ('工程质量技术部', down, '分包工程验收记录、质量扣款单', 28, '质量扣款依据，不合格不结算'),
        ('安全环保部', down, '安全违规处罚单、文明施工考核扣分', 28, '安全扣款依据'),
        ('设备物资部', down, '甲供材领用台账、超耗扣款表、分包代采物资计量单', 28, '材料扣款'),
        ('人力资源部', down, '工资代发清单（即分包用菲籍工人）、考勤抽查记录、自用人工费', 28, '工资发放核查，防欠薪纠纷'),
        ('综合办公室', down, '水电、住宿、食堂等后勤扣款', 28, '现场经费扣款'),
        ('财务资金部', down, '已付款台账、预付款扣回情况', 28, '付款控制，防止超付'),
    ]

    for dept_name, st_id, materials, deadline, remarks in upstream + downstream:
        dept = db.execute("SELECT id FROM departments WHERE name=?", (dept_name,)).fetchone()
        if dept:
            db.execute('''
                INSERT INTO task_configs (department_id, settlement_type_id, required_materials, deadline_day, remarks, is_active)
                VALUES (?,?,?,?,?,1)
                ON CONFLICT DO NOTHING
            ''', (dept[0], st_id, materials, deadline, remarks))

    db.commit()
