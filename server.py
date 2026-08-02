# -*- coding: utf-8 -*-
"""
生产级服务器启动脚本
使用 waitress 作为 WSGI 服务器，支持长期稳定运行
"""

import os
import sys
import shutil
import logging
import threading
import time
from datetime import datetime, date, timedelta
from logging.handlers import TimedRotatingFileHandler

# 确保在正确目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

# 日志配置
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

log_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'server.log'),
    when='midnight',
    backupCount=30,
    encoding='utf-8'
)
log_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s'
))
logging.basicConfig(level=logging.INFO, handlers=[log_handler])
logger = logging.getLogger(__name__)

# 数据库备份
DB_PATH = os.path.join(BASE_DIR, 'data', 'settlement.db')
BACKUP_DIR = os.path.join(BASE_DIR, 'data', 'backups')

def auto_backup_db():
    """自动备份数据库，保留最近30天"""
    if not os.path.exists(DB_PATH):
        return False
    os.makedirs(BACKUP_DIR, exist_ok=True)
    today = date.today().strftime('%Y-%m-%d')
    backup_file = os.path.join(BACKUP_DIR, f'settlement_{today}.db')
    if not os.path.exists(backup_file):
        shutil.copy2(DB_PATH, backup_file)
        logger.info(f'数据库已备份到 {backup_file}')
        # 清理30天前的备份
        cutoff = datetime.now().toordinal() - 30
        for f in os.listdir(BACKUP_DIR):
            fpath = os.path.join(BACKUP_DIR, f)
            if f.endswith('.db') and os.path.isfile(fpath):
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).toordinal()
                if mtime < cutoff:
                    os.remove(fpath)
                    logger.info(f'清理旧备份: {f}')
        return True
    else:
        logger.info('今日已备份，跳过')
        return False


def daily_backup_thread():
    """后台线程：每天凌晨2点自动备份数据库"""
    logger.info('每日自动备份线程已启动 (每天 02:00)')
    while True:
        # 计算到明天凌晨2点的等待时间
        now = datetime.now()
        tomorrow_2am = datetime(now.year, now.month, now.day) + timedelta(days=1, hours=2)
        wait_seconds = (tomorrow_2am - now).total_seconds()
        logger.info(f'下次自动备份: {tomorrow_2am.strftime("%Y-%m-%d %H:%M")} ({int(wait_seconds)}秒后)')
        time.sleep(wait_seconds)
        try:
            logger.info('开始执行每日自动备份...')
            auto_backup_db()
        except Exception as e:
            logger.error(f'每日备份失败: {e}')


def main():
    from app import app, init_db

    # 初始化数据库
    init_db()
    logger.info('数据库初始化完成')

    # 启动时自动备份
    auto_backup_db()

    # 启动每日备份后台线程
    backup_t = threading.Thread(target=daily_backup_thread, daemon=True)
    backup_t.start()

    # 使用 waitress 启动
    try:
        from waitress import serve
        logger.info('使用 waitress 生产服务器启动')
        print('=' * 60)
        print('  帕基尔项目月度结算管理系统 (生产模式)')
        print('  访问地址: http://localhost:5000')
        print('  局域网访问: http://<本机IP>:5000')
        print('  日志文件: logs/server.log')
        print('  数据备份: data/backups/')
        print('  自动备份: 每天 02:00')
        print('=' * 60)
        print()
        print('  按 Ctrl+C 停止服务')
        print()
        serve(app, host='0.0.0.0', port=5000, threads=8)
    except ImportError:
        logger.warning('waitress 未安装，回退到 Flask 开发服务器')
        print('[警告] waitress 未安装，使用 Flask 开发服务器')
        app.run(host='0.0.0.0', port=5000, debug=False)

if __name__ == '__main__':
    main()
