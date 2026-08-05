# 终极部署脚本 v2 - 解决所有问题
# 1. 先在脚本里把 node.exe 复制到 vercel.cmd 同目录
# 2. 然后用 --token 参数直接部署

$nodeSrc = "C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2\node.exe"
$binDir = "C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules\.bin"
$nodeDst = "$binDir\node.exe"
$vercelCmd = "$binDir\vercel.cmd"

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Pakil Settlement - Ultimate Deploy" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# 第 1 步：复制 node.exe 到 vercel.cmd 同目录
# 这样 vercel.cmd 内部 SETLOCAL 找 node.exe 时能直接找到，不需要 PATH
if (-not (Test-Path $nodeDst)) {
    Write-Host "[1/3] Copying node.exe to vercel.cmd directory..." -ForegroundColor Yellow
    Copy-Item $nodeSrc $nodeDst -Force
    Write-Host "      [OK] node.exe copied" -ForegroundColor Green
} else {
    Write-Host "[1/3] node.exe already in place" -ForegroundColor Yellow
}
Write-Host ""

# 第 2 步：让用户输入 token
Write-Host "[2/3] Need your Vercel Token" -ForegroundColor Yellow
Write-Host "      Get from: https://vercel.com/account/tokens" -ForegroundColor Gray
Write-Host ""
$token = Read-Host "Paste your Vercel Token"
Write-Host ""

if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Host "[ERROR] Token is empty" -ForegroundColor Red
    exit 1
}

# 保存 token
$token | Out-File "$env:USERPROFILE\.vercel-token" -Encoding utf8 -NoNewline

# 第 3 步：直接部署
Set-Location "C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system"
Write-Host "[3/3] Deploying to production..." -ForegroundColor Yellow
Write-Host "      (If asked to select team, choose tiangjhu01-1213-)" -ForegroundColor Gray
Write-Host ""

& $vercelCmd deploy --prod --yes --token $token --project settlement_system

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " DONE! Check https://settlementsystem.vercel.app" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan