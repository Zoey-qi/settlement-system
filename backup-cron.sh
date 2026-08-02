#!/bin/bash
# ============================================================
# 数据库自动备份脚本 - 配合 crontab 使用
# 建议每天凌晨2点执行: crontab -e
# 添加: 0 2 * * * /bin/bash /app/backup-cron.sh
# ============================================================

BACKUP_DIR="/app/data/backups"
DB_FILE="/app/data/settlement.db"
DATE=$(date +%Y-%m-%d)
BACKUP_FILE="${BACKUP_DIR}/settlement_${DATE}.db"

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_FILE" ]; then
    cp "$DB_FILE" "$BACKUP_FILE"
    echo "[$DATE] 数据库已备份到 $BACKUP_FILE"
    
    # 清理30天前的备份
    find "$BACKUP_DIR" -name "settlement_*.db" -mtime +30 -delete
    echo "[$DATE] 已清理30天前的旧备份"
else
    echo "[$DATE] 警告: 数据库文件不存在"
fi
