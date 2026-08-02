# -*- coding: utf-8 -*-
"""Vercel Serverless 入口"""
from app import app, init_db

# 冷启动时初始化数据库（幂等操作）
init_db()

# Vercel 自动识别此变量
app = app
