# -*- coding: utf-8 -*-
"""
导入已有模板文件到系统数据库
"""

import os
import sys
import sqlite3
import shutil
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'template_files')
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'settlement.db')

# 已有模板文件路径
SOURCE_DIR = r'D:\水电基础局\Pakil项目资料\17-其他\菲律宾帕基尔抽蓄基础处理工程项目结算管理'

# 模板文件映射: (源文件名, 显示名称, 结算类型, 部门, 说明)
TEMPLATE_FILES = [
    # 对上结算附件
    ('对上结算附件/Pakil抽水蓄能电站项目 计日工套表对上.xlsx', '计日工套表（对上）', 'upstream', '通用', '对上结算计日工套表'),
    ('对上结算附件/对上工程量确认单：工程质量技术部.xlsx', '对上工程量确认单', 'upstream', '工程质量技术部', '工程质量技术部对上结算工程量确认单'),
    ('对上结算附件/对上结算：综合办公室.xlsx', '对上结算-综合办公室', 'upstream', '综合办公室', '综合办公室对上结算附件'),
    ('对上结算附件/对上结算附件-设备物资部 .xlsx', '对上结算-设备物资部', 'upstream', '设备物资部', '设备物资部对上结算附件'),
    ('对上结算附件/罚款 HSE：安全环保部.xlsx', 'HSE罚款单', 'upstream', '安全环保部', '安全环保部HSE罚款单'),
    ('对上结算附件/设备（设备物资部&生产调度部）.xlsx', '设备使用确认单', 'upstream', '设备物资部', '设备物资部&生产调度部设备使用确认单'),
    # 对下分包结算附件
    ('对下分包结算附件/对下工程量确认单（工程质量技术部）.xlsx', '对下工程量确认单', 'downstream', '工程质量技术部', '工程质量技术部分包工程量确认单'),
    ('对下分包结算附件/对下技术质量部扣款（工程质量技术部）.xlsx', '对下技术质量部扣款单', 'downstream', '工程质量技术部', '工程质量技术部分包扣款计量单'),
    ('对下分包结算附件/安全环保部扣款（安全环保部）.xlsx', '安全环保部扣款单', 'downstream', '安全环保部', '安全环保部分包扣款计量单'),
    ('对下分包结算附件/材料扣款（设备物资部）.xlsx', '材料扣款单', 'downstream', '设备物资部', '设备物资部分包代采物资计量单'),
    ('对下分包结算附件/借用设备确认单（生产调度部&设备物资部）.xlsx', '借用设备确认单', 'downstream', '生产调度部', '生产调度部&设备物资部借用设备确认单'),
    ('对下分包结算附件/代付工资 (综合办公室).xlsx', '代付工资扣款单', 'downstream', '综合办公室', '综合办公室分包商代付工资扣款'),
    ('对下分包结算附件/餐费（综合办公室）.xlsx', '餐费扣款单', 'downstream', '综合办公室', '综合办公室分包商伙食费用扣款单'),
    ('对下分包结算附件/住宿费（综合办公室） .xlsx', '住宿费扣款单', 'downstream', '综合办公室', '综合办公室分包商住宿扣款单'),
    ('对下分包结算附件/代付签证 （若有).xlsx', '代付签证扣款单', 'downstream', '综合办公室', '综合办公室分包商代付签证扣款（若有）'),
]

ST_NAMES = {
    'upstream': '对上结算',
    'downstream': '对下结算',
    'common': '通用'
}


def import_templates():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 检查是否已导入
    existing = conn.execute('SELECT COUNT(*) as c FROM template_files').fetchone()['c']
    if existing > 0:
        print(f'模板文件已存在({existing}个)，跳过导入。如需重新导入请先清空template_files表。')
        conn.close()
        return

    imported = 0
    for rel_path, name, st_type, dept, desc in TEMPLATE_FILES:
        src = os.path.join(SOURCE_DIR, rel_path)
        # 尝试处理文件名中的空格
        if not os.path.exists(src):
            # 尝试去掉文件名中的空格
            src_stripped = src.replace(' .xlsx', '.xlsx').replace(' .xls', '.xls')
            if os.path.exists(src_stripped):
                src = src_stripped
            else:
                print(f'  [跳过] 文件不存在: {rel_path}')
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
            print(f'  [成功] {name} ({file_size//1024}KB)')
        except Exception as e:
            print(f'  [失败] {name}: {e}')

    conn.commit()
    conn.close()
    print(f'\n导入完成！共导入 {imported} 个模板文件。')


if __name__ == '__main__':
    print('正在导入已有模板文件...')
    import_templates()
