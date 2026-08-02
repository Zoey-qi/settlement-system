# -*- coding: utf-8 -*-
"""
数据库自动备份脚本
可由 Windows 任务计划程序每日执行
保留最近30天的备份
"""

import os
import shutil
from datetime import datetime, date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'settlement.db')
BACKUP_DIR = os.path.join(BASE_DIR, 'data', 'backups')

def backup():
    if not os.path.exists(DB_PATH):
        print('数据库文件不存在，跳过备份')
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    today = date.today().strftime('%Y-%m-%d')
    backup_file = os.path.join(BACKUP_DIR, f'settlement_{today}.db')

    # 如果今天已备份则跳过
    if os.path.exists(backup_file):
        print(f'今日已备份: {backup_file}')
        return

    shutil.copy2(DB_PATH, backup_file)
    print(f'备份完成: {backup_file}')

    # 清理30天前的备份
    cutoff = datetime.now().toordinal() - 30
    cleaned = 0
    for f in os.listdir(BACKUP_DIR):
        fpath = os.path.join(BACKUP_DIR, f)
        if f.endswith('.db') and os.path.isfile(fpath):
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).toordinal()
            if mtime < cutoff:
                os.remove(fpath)
                cleaned += 1
    if cleaned:
        print(f'清理 {cleaned} 个过期备份')

if __name__ == '__main__':
    backup()
