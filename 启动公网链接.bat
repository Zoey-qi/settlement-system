@echo off
chcp 65001 >nul
title 帕基尔结算系统 - 公网链接
echo.
echo ============================================
echo   帕基尔结算系统 - 公网链接启动
echo ============================================
echo.

REM 检查系统是否已在运行
netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if %errorlevel% neq 0 (
    echo [1/2] 启动结算系统服务...
    cd /d "%~dp0"
    start /b "" "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" server.py
    echo       等待服务启动...
    timeout /t 4 >nul
    echo       服务已启动
) else (
    echo [1/2] 结算系统已在运行，跳过
)

echo [2/2] 建立公网隧道...
echo.
echo ============================================
echo.
echo   固定公网链接：
echo.
echo   https://441121ff92da9a.lhr.life
echo.
echo   把这个链接发给别人，浏览器打开就能用
echo   手机也能访问
echo   每次启动都是同一个链接，不会变！
echo.
echo   *** 不要关闭此窗口！关闭则链接失效 ***
echo.
echo ============================================
echo.

ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -i %USERPROFILE%\.ssh\id_ed25519 -R 80:localhost:5000 plan@localhost.run

echo.
echo 隧道已断开，按任意键退出...
pause >nul
