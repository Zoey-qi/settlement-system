# 纯命令行版 Vercel 部署（无需浏览器登录）
# 用 Vercel Deploy Token（一次性生成，存到 .vercel-token）

$tokenFile = "$env:USERPROFILE\.vercel-token"
$nodeExe = "C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2\node.exe"
$vercelCmd = "C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules\.bin\vercel.cmd"

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "   Vercel 纯命令行部署（需要 Deploy Token）" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""

# 检查 token 文件
if (-not (Test-Path $tokenFile)) {
    Write-Host "[!] 还没生成 Vercel Deploy Token" -ForegroundColor Red
    Write-Host ""
    Write-Host "请按以下步骤获取 Token：" -ForegroundColor Yellow
    Write-Host "  1. 浏览器打开 https://vercel.com/account/tokens" -ForegroundColor White
    Write-Host "  2. 点击 'Create Token'，名字随便（如 'settlement-cli'）" -ForegroundColor White
    Write-Host "  3. Scope 选 'Full Account' 或 'This Team'" -ForegroundColor White
    Write-Host "  4. 过期时间选 '1 day' 或更长" -ForegroundColor White
    Write-Host "  5. 点击 Create，**复制生成的 token**（只显示一次）" -ForegroundColor White
    Write-Host ""
    $token = Read-Host "请粘贴你的 Vercel Token"
    Write-Host ""
    $token | Out-File -FilePath $tokenFile -Encoding utf8 -NoNewline
    Write-Host "[OK] Token 已保存到 $tokenFile" -ForegroundColor Green
    Write-Host ""
}

# 读取 token
$token = Get-Content $tokenFile -Raw

# 设置环境变量
$env:VERCEL_TOKEN = $token.Trim()

# 切换到项目目录
Set-Location "C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system"

# 部署到生产环境
Write-Host "[1/1] 部署到生产环境..." -ForegroundColor Yellow
& $vercelCmd deploy --prod --yes --token $token --confirm

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host " 部署完成！" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan