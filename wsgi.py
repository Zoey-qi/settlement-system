# -*- coding: utf-8 -*-
"""
WSGI 入口文件 - 用于 gunicorn 生产级部署（Linux）
"""

from app import app, init_db

# 启动时初始化数据库
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
