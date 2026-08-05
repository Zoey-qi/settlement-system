# ========================================================
# Vercel CLI 部署脚本（简化版，避免 PowerShell 解析问题）
# ========================================================

$ErrorActionPreference = "Stop"
Set-Location "C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system"

Write-Host ""
Write-Host "=== Step 1: 检查环境 ===" -ForegroundColor Cyan
node --version
npm --version

Write-Host ""
Write-Host "=== Step 2: 安装 Vercel CLI ===" -ForegroundColor Cyan
$vercelInstalled = Get-Command vercel -ErrorAction SilentlyContinue
if (-not $vercelInstalled) {
    Write-Host "正在安装 Vercel CLI..."
    npm install -g vercel
} else {
    Write-Host "Vercel CLI 已安装"
}

Write-Host ""
Write-Host "=== Step 3: 检查登录 ===" -ForegroundColor Cyan
$who = vercel whoami 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "请在浏览器登录 Vercel（用团队 tiangjhu01-1213 下的账号）"
    vercel login
}

Write-Host ""
Write-Host "=== Step 4: 链接项目 ===" -ForegroundColor Cyan
vercel link --yes

Write-Host ""
Write-Host "=== Step 5: 部署到生产环境 ===" -ForegroundColor Cyan
Write-Host "接下来会问 'Want to modify these settings?'，直接按 Enter 接受默认"
Write-Host ""
vercel deploy --prod --yes

Write-Host ""
Write-Host "=== 完成 ===" -ForegroundColor Green
Write-Host "已部署到 https://settlementsystem.vercel.app/"
Start-Process "https://settlementsystem.vercel.app/"
