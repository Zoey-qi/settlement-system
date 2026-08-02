#!/bin/bash
# ============================================================
# 帕基尔项目月度结算系统 - 一键部署脚本
# 在 Linux 云服务器上运行此脚本即可完成部署
# ============================================================

set -e

echo ""
echo "============================================"
echo "  帕基尔项目月度结算系统 - 一键部署"
echo "============================================"
echo ""

# 检查是否以 root 用户运行
if [ "$EUID" -ne 0 ]; then
    echo "[提示] 建议使用 root 用户运行，或加 sudo"
    SUDO="sudo"
else
    SUDO=""
fi

# 检查 Docker 是否已安装
echo "[1/4] 检查 Docker..."
if ! command -v docker &> /dev/null; then
    echo "  Docker 未安装，正在自动安装..."
    curl -fsSL https://get.docker.com | $SUDO sh
    $SUDO systemctl start docker
    $SUDO systemctl enable docker
    echo "  Docker 安装完成"
else
    echo "  Docker 已安装"
    $SUDO systemctl start docker 2>/dev/null || true
fi

# 检查 Docker Compose 是否可用
echo "[2/4] 检查 Docker Compose..."
if docker compose version &> /dev/null; then
    echo "  Docker Compose 已就绪"
elif command -v docker-compose &> /dev/null; then
    echo "  docker-compose 已安装"
    # 创建别名
    alias docker-compose="docker compose"
else
    echo "  安装 Docker Compose 插件..."
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq docker-compose-plugin 2>/dev/null || true
fi

# 构建并启动
echo "[3/4] 构建并启动容器..."
$SUDO docker compose down 2>/dev/null || true
$SUDO docker compose up -d --build

# 等待容器启动
echo "  等待服务启动..."
sleep 3

# 检查状态
echo "[4/4] 检查运行状态..."
if $SUDO docker ps | grep -q pakil-settlement; then
    # 获取服务器公网IP
    PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s ip.sb 2>/dev/null || echo "服务器IP")
    
    echo ""
    echo "============================================"
    echo "  部署成功！"
    echo "============================================"
    echo ""
    echo "  访问地址:  http://${PUBLIC_IP}:5000"
    echo ""
    echo "  局域网内:  http://服务器内网IP:5000"
    echo ""
    echo "  常用命令:"
    echo "    查看日志:   sudo docker compose logs -f"
    echo "    重启服务:   sudo docker compose restart"
    echo "    停止服务:   sudo docker compose down"
    echo "    更新部署:   重新上传文件后运行 bash deploy.sh"
    echo ""
    echo "  数据备份位置: ./data/backups/"
    echo "  日志位置:     ./logs/"
    echo ""
    echo "============================================"
    
    # 测试健康检查
    if curl -s http://localhost:5000/health | grep -q "ok\|健康\|200"; then
        echo "  健康检查: 通过"
    else
        echo "  [提示] 服务正在启动中，请等待几秒后访问"
    fi
    echo "============================================"
    echo ""
else
    echo ""
    echo "  [错误] 容器启动失败，请查看日志："
    echo "  sudo docker compose logs"
    echo ""
    exit 1
fi
