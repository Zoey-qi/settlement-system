@echo off
chcp 65001 >nul
title 帕基尔项目月度结算管理系统

echo ============================================================
echo   菲律宾帕基尔抽蓄基础处理工程项目
echo   月度结算数据管理系统
echo ============================================================
echo.

cd /d "%~dp0"

set PYTHON=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe

if not exist "%PYTHON%" (
    echo [错误] 未找到Python环境，请检查路径: %PYTHON%
    pause
    exit /b 1
)

echo [1/3] 初始化数据库并自动备份...
"%PYTHON%" -c "from app import init_db; init_db(); print('  数据库初始化完成')"
"%PYTHON%" backup_db.py

echo [2/3] 检测本机局域网IP...
for /f "tokens=*" %%i in ('"%PYTHON% -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(s.getsockname()[0]); s.close()""') do set LAN_IP=%%i
echo   局域网IP: %LAN_IP%

echo [3/3] 启动Web服务...
echo.
echo ============================================================
echo.
echo   系统已启动！把下面这个链接发给同事：
echo.
echo   http://%LAN_IP%:5000
echo.
echo   同事在浏览器打开就能上传操作，手机也行。
echo   你自己访问: http://localhost:5000
echo.
echo   *** 不要关闭此窗口！关闭则系统停止 ***
echo.
echo   日志文件: logs\server.log
echo   数据备份: data\backups\
echo.
echo ============================================================
echo.

"%PYTHON%" server.py

pause
