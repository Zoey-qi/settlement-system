# -*- coding: utf-8 -*-
"""
重新导入模板文件（第三次）
- 清空旧模板
- 从源目录导入所有最新模板
- 重新关联条目
"""

import os
import shutil
import sqlite3
import openpyxl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'settlement.db')
TEMPLATE_DIR = os.path.join(BASE_DIR, 'template_files')
SOURCE_BASE = r'D:\水电基础局\Pakil项目资料\17-其他\菲律宾帕基尔抽蓄基础处理工程项目结算管理'

# 模板来源目录映射
TEMPLATE_SOURCES = [
    # (目录路径, 结算类型, 部门)
    (os.path.join(SOURCE_BASE, '对上结算附件'), '对上结算', None),
    (os.path.join(SOURCE_BASE, '对上结算附件', '对上结算：综合办公室'), '对上结算', '综合办公室'),
    (os.path.join(SOURCE_BASE, '对下分包结算附件'), '对下结算', None),
]


def collect_templates():
    """收集所有模板文件"""
    templates = []
    for dir_path, st_type, default_dept in TEMPLATE_SOURCES:
        if not os.path.exists(dir_path):
            print(f'  [跳过] 目录不存在: {dir_path}')
            continue
        for f in sorted(os.listdir(dir_path)):
            if not f.endswith('.xlsx') or f.startswith('~'):
                continue
            fpath = os.path.join(dir_path, f)
            # 推断部门
            dept = default_dept
            if '工程质量技术部' in f:
                dept = '工程质量技术部'
            elif '安全环保部' in f or 'HSE' in f:
                dept = '安全环保部'
            elif '设备物资部' in f:
                dept = '设备物资部'
            elif '综合办公室' in f:
                dept = '综合办公室'
            elif '生产调度部' in f:
                dept = '生产调度部'
            elif '计日工' in f:
                dept = '通用'

            # 推断名称
            name = os.path.splitext(f)[0]
            # 清理名称
            if st_type == '对上结算':
                name = f'对上-{name}'
            else:
                name = f'对下-{name}'

            templates.append({
                'source_path': fpath,
                'original_name': f,
                'name': name,
                'settlement_type': st_type,
                'department': dept or '通用',
            })
    return templates


def reimport():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 1. 解除所有条目的模板关联
    c.execute('UPDATE task_items SET template_file_id = NULL')
    print('已解除所有条目模板关联')

    # 2. 删除旧模板文件记录和物理文件
    old_templates = c.execute('SELECT * FROM template_files').fetchall()
    for tpl in old_templates:
        old_file = os.path.join(TEMPLATE_DIR, tpl['stored_name'])
        if os.path.exists(old_file):
            os.remove(old_file)
    c.execute('DELETE FROM template_files')
    conn.commit()
    print(f'已删除 {len(old_templates)} 个旧模板')

    # 3. 收集并导入新模板
    templates = collect_templates()
    print(f'\n找到 {len(templates)} 个模板文件待导入:')

    import datetime
    timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')

    for i, tpl in enumerate(templates):
        # 生成存储文件名
        stored_name = f"{timestamp}_{i}_{tpl['original_name']}"
        dest_path = os.path.join(TEMPLATE_DIR, stored_name)
        shutil.copy2(tpl['source_path'], dest_path)
        file_size = os.path.getsize(dest_path)

        c.execute('''
            INSERT INTO template_files (name, description, settlement_type, department, file_name, stored_name, file_size, uploader)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (
            tpl['name'],
            '',
            tpl['settlement_type'],
            tpl['department'],
            tpl['original_name'],
            stored_name,
            file_size,
            '管理员'
        ))
        print(f'  [{i}] {tpl["name"]} ({tpl["settlement_type"]}/{tpl["department"]})')

    conn.commit()

    # 4. 获取新模板ID映射
    new_templates = c.execute('SELECT * FROM template_files ORDER BY id').fetchall()
    print(f'\n导入完成，共 {len(new_templates)} 个模板')

    # 5. 手动精准关联条目
    # 定义条目名称关键词 -> 模板名称关键词的映射
    LINK_RULES = [
        # 对上
        ('当月完成工程量报表', '对上-对上工程量确认单'),
        ('质量验收记录', '对上-对上工程量确认单'),
        ('工程量确认', '对上-对上工程量确认单'),
        ('HSE罚款', '对上-对上提交罚款'),
        ('分包商材料领用表', '对上-对上结算附件-设备物资部'),
        ('安全领用材料', '对上-对上结算附件-设备物资部'),
        ('设备使用扣款单', '对上-对上设备'),
        ('材料调拨单', '对上-对上结算附件-设备物资部'),
        ('农民工工资发放表', '对上-对下代付工资'),  # 复用工资源模板
        ('分包人员宿舍租赁', '对上-对上：住宿费'),
        ('水电费', '对上-对上：生活用水'),  # 默认关联水费
        ('食堂伙食费扣款单', '对上-对上：水电基础局伙食扣款单'),
        # 对下
        ('分包当月完成工程量核定表', '对下-对下工程量确认单'),
        ('分包工程验收记录', '对下-对下工程量确认单'),
        ('质量扣款单', '对下-对下技术质量部扣款'),
        ('安全违规处罚单', '对下-对下HSE扣款'),
        ('文明施工考核扣分', '对下-对下HSE扣款'),
        ('甲供材领用台账', '对下-对下材料扣款'),
        ('超耗扣款表', '对下-对下材料扣款'),
        ('分包代采物资计量单', '对下-对下材料扣款'),
        ('工资代发清单', '对下-对下代付工资'),
        ('分包水电费', '对下-对下：生活用水'),  # 新模板
        ('分包住宿费用', '对下-对下住宿费'),
        ('食堂等后勤扣款', '对下-对下餐费'),
    ]

    # 构建模板名称查找
    tpl_by_name = {t['name']: t for t in new_templates}

    linked_count = 0
    items = c.execute('SELECT * FROM task_items WHERE is_active = 1').fetchall()
    for item in items:
        item_name = item['item_name']
        best_tpl_id = None

        for keyword, tpl_name_key in LINK_RULES:
            if keyword in item_name:
                # 找匹配的模板
                for tpl in new_templates:
                    if tpl_name_key in tpl['name']:
                        best_tpl_id = tpl['id']
                        break
                if best_tpl_id:
                    break

        if best_tpl_id:
            c.execute('UPDATE task_items SET template_file_id = ? WHERE id = ?',
                     (best_tpl_id, item['id']))
            linked_count += 1
            print(f'  关联: [{item["id"]}] {item_name} -> {tpl_by_name.get(best_tpl_id, {}).get("name", "?") if best_tpl_id else "无"}')

    conn.commit()
    print(f'\n关联完成: {linked_count}/{len(items)} 个条目已关联模板')

    # 打印未关联的条目
    unlinked = c.execute('''
        SELECT ti.id, ti.item_name, d.name as dept, st.name as st_name
        FROM task_items ti
        JOIN task_configs tc ON ti.task_config_id = tc.id
        JOIN departments d ON tc.department_id = d.id
        JOIN settlement_types st ON tc.settlement_type_id = st.id
        WHERE ti.template_file_id IS NULL AND ti.is_active = 1
        ORDER BY ti.id
    ''').fetchall()
    if unlinked:
        print(f'\n未关联模板的条目 ({len(unlinked)} 个):')
        for u in unlinked:
            print(f'  [{u["id"]}] {u["st_name"]} | {u["dept"]} | {u["item_name"]}')

    conn.close()
    print('\n模板重新导入完成!')


if __name__ == '__main__':
    reimport()
