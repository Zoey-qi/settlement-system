# 帕基尔项目 Vercel 一键部署脚本 v3（绝对路径 + 修 PATH）
# 解决 vercel.cmd 调用 node 时找不到 node 的问题

$nodePath = "C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2"
$nodeExe = "$nodePath\node.exe"
$npmCmd = "$nodePath\npm.cmd"
$vercelCmd = "C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules\.bin\vercel.cmd"

# 关键修复：在脚本启动时把 node 路径加到 PATH 前面
# 这样 vercel.cmd 内部 %dp0\..\node\node.exe 才能被找到
$env:Path = "$nodePath;$env:Path"

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "   Pakil Vercel Deploy v3 (absolute path)" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""

# 切到项目目录
Set-Location "C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system"
Write-Host "[OK] in project dir" -ForegroundColor Green
Write-Host ""

# 验证工具
Write-Host "[1/4] check tools..." -ForegroundColor Yellow
& $nodeExe --version
& $npmCmd --version
& $vercelCmd --version
Write-Host ""

# 登录 Vercel（弹浏览器，用 tiangjhu01-1213 团队账号）
Write-Host "[2/4] login to Vercel (browser will open, use tiangjhu01-1213 team account)..." -ForegroundColor Yellow
& $vercelCmd login
Write-Host ""

# 关联项目
Write-Host "[3/4] link project..." -ForegroundColor Yellow
& $vercelCmd link --yes
Write-Host ""

# 部署到生产环境
Write-Host "[4/4] deploy to production..." -ForegroundColor Yellow
& $vercelCmd deploy --prod --yes

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host " Done! Open https://settlementsystem.vercel.app" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan