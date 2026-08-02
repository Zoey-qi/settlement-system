# -*- coding: utf-8 -*-
"""
重新导入模板文件 - 清空旧模板后全量重新导入
"""

import os
import sys
import glob
import sqlite3
import shutil
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'template_files')
DB_PATH = os.path.join(BASE_DIR, 'data', 'settlement.db')

SOURCE_DIR = r'D:\水电基础局\Pakil项目资料\17-其他\菲律宾帕基尔抽蓄基础处理工程项目结算管理'

# 重新整理后的模板文件映射: (相对路径, 显示名称, 结算类型, 部门, 说明)
TEMPLATE_FILES = [
    # ===== 对上结算附件 =====
    ('对上结算附件/对上签证Pakil抽水蓄能电站项目 计日工套表对上.xlsx',
     '计日工套表（对上）', 'upstream', '通用', '对上结算计日工套表/签证'),
    ('对上结算附件/对上工程量确认单：工程质量技术部.xlsx',
     '对上工程量确认单', 'upstream', '工程质量技术部', '工程质量技术部对上结算工程量确认单'),
    ('对上结算附件/对上提交罚款 HSE：安全环保部.xlsx',
     'HSE罚款单（对上）', 'upstream', '安全环保部', '安全环保部对上HSE罚款单'),
    ('对上结算附件/对上结算附件-设备物资部 .xlsx',
     '对上结算-设备物资部', 'upstream', '设备物资部', '设备物资部对上结算附件'),
    ('对上结算附件/对上设备（设备物资部&生产调度部）.xlsx',
     '设备使用确认单（对上）', 'upstream', '设备物资部', '设备物资部&生产调度部对上设备使用确认单'),
    # 综合办公室对上拆分为4个独立模板
    ('对上结算附件/对上结算：综合办公室/对上：住宿费（综合办公室）.xlsx',
     '对上-住宿费扣款单', 'upstream', '综合办公室', '综合办公室对上结算住宿费扣款'),
    ('对上结算附件/对上结算：综合办公室/对上：水电基础局伙食扣款单截至7月23日（综合办公室）.xlsx',
     '对上-伙食费扣款单', 'upstream', '综合办公室', '综合办公室对上结算伙食费扣款'),
    ('对上结算附件/对上结算：综合办公室/对上：生活用水（综合办公室）.xlsx',
     '对上-水费扣款单', 'upstream', '综合办公室', '综合办公室对上结算生活用水扣款'),
    ('对上结算附件/对上结算：综合办公室/对上：生活用电（综合办公室）.xlsx',
     '对上-电费扣款单', 'upstream', '综合办公室', '综合办公室对上结算生活用电扣款'),

    # ===== 对下分包结算附件 =====
    ('对下分包结算附件/对下工程量确认单（工程质量技术部）.xlsx',
     '对下工程量确认单', 'downstream', '工程质量技术部', '工程质量技术部分包工程量确认单'),
    ('对下分包结算附件/对下技术质量部扣款（工程质量技术部）.xlsx',
     '对下技术质量部扣款单', 'downstream', '工程质量技术部', '工程质量技术部分包扣款计量单'),
    ('对下分包结算附件/对下HSE扣款（安全环保部）.xlsx',
     '对下HSE扣款单', 'downstream', '安全环保部', '安全环保部分包HSE扣款计量单'),
    ('对下分包结算附件/对下材料扣款（设备物资部）.xlsx',
     '对下材料扣款单', 'downstream', '设备物资部', '设备物资部分包代采物资/材料扣款计量单'),
    ('对下分包结算附件/对下借用设备确认单（生产调度部&设备物资部）.xlsx',
     '对下借用设备确认单', 'downstream', '生产调度部', '生产调度部&设备物资部分包借用设备确认单'),
    ('对下分包结算附件/对下代付工资 (综合办公室).xlsx',
     '对下代付工资扣款单', 'downstream', '综合办公室', '综合办公室分包商代付工资扣款'),
    ('对下分包结算附件/对下餐费（综合办公室）.xlsx',
     '对下餐费扣款单', 'downstream', '综合办公室', '综合办公室分包商伙食费用扣款单'),
    ('对下分包结算附件/对下住宿费（综合办公室）.xlsx',
     '对下住宿费扣款单', 'downstream', '综合办公室', '综合办公室分包商住宿扣款单'),
    ('对下分包结算附件/代付签证 （若有).xlsx',
     '代付签证扣款单', 'downstream', '综合办公室', '综合办公室分包商代付签证扣款（若有）'),
]

ST_NAMES = {
    'upstream': '对上结算',
    'downstream': '对下结算',
    'common': '通用'
}


def find_file(rel_path):
    """查找文件，处理文件名中的空格差异"""
    src = os.path.join(SOURCE_DIR, rel_path)
    if os.path.exists(src):
        return src
    # 尝试去掉空格
    src_stripped = src.replace(' .xlsx', '.xlsx').replace(' .xls', '.xls')
    if os.path.exists(src_stripped):
        return src_stripped
    return None


def clear_old_templates(conn):
    """清空旧模板：DB记录 + 物理文件 + 条目关联"""
    # 1. 清除条目关联
    conn.execute('UPDATE task_items SET template_file_id = NULL')
    # 2. 删除DB记录
    conn.execute('DELETE FROM template_files')
    conn.commit()

    # 3. 删除物理文件
    old_files = glob.glob(os.path.join(TEMPLATE_DIR, '*'))
    for f in old_files:
        try:
            os.remove(f)
        except Exception as e:
            print(f'  [警告] 无法删除: {os.path.basename(f)}: {e}')

    print(f'已清空旧模板（{len(old_files)}个物理文件）')


def import_templates():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print('=' * 60)
    print('重新导入模板文件')
    print('=' * 60)

    # 清空旧模板
    clear_old_templates(conn)

    print(f'\n开始导入 {len(TEMPLATE_FILES)} 个模板文件...\n')

    imported = 0
    skipped = 0
    for rel_path, name, st_type, dept, desc in TEMPLATE_FILES:
        src = find_file(rel_path)
        if not src:
            print(f'  [跳过] 文件不存在: {rel_path}')
            skipped += 1
            continue

        filename = os.path.basename(src)
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        stored_name = f"{timestamp}_{imported}_{filename}"
        dst = os.path.join(TEMPLATE_DIR, stored_name)

        try:
            shutil.copy2(src, dst)
            file_size = os.path.getsize(dst)

            conn.execute('''
                INSERT INTO template_files (name, description, settlement_type, department, file_name, stored_name, file_size, uploader)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (name, desc, ST_NAMES.get(st_type, '通用'), dept, filename, stored_name, file_size, '系统导入'))
            imported += 1
            print(f'  [成功] {name} ({file_size // 1024}KB)')
        except Exception as e:
            print(f'  [失败] {name}: {e}')
            skipped += 1

    conn.commit()

    # 重新自动关联模板
    print('\n正在自动关联条目与模板...')
    from app import auto_link_templates
    auto_link_templates(conn)

    # 验证关联结果
    linked = conn.execute('SELECT COUNT(*) as c FROM task_items WHERE template_file_id IS NOT NULL').fetchone()['c']
    total_items = conn.execute('SELECT COUNT(*) as c FROM task_items').fetchone()['c']
    total_templates = conn.execute('SELECT COUNT(*) as c FROM template_files').fetchone()['c']

    conn.close()

    print(f'\n{"=" * 60}')
    print(f'导入完成！')
    print(f'  模板文件: {imported} 个导入成功, {skipped} 个跳过')
    print(f'  数据库模板总数: {total_templates}')
    print(f'  条目自动关联: {linked}/{total_items}')
    print(f'{"=" * 60}')

    # 打印关联详情
    conn2 = sqlite3.connect(DB_PATH)
    conn2.row_factory = sqlite3.Row
    links = conn2.execute('''
        SELECT ti.item_name, tf.name as tpl_name
        FROM task_items ti
        JOIN template_files tf ON ti.template_file_id = tf.id
        ORDER BY ti.id
    ''').fetchall()
    if links:
        print('\n已关联的条目:')
        for li in links:
            print(f'  {li["item_name"]}  ->  {li["tpl_name"]}')
    conn2.close()


if __name__ == '__main__':
    import_templates()
