# ============================================================================
#  CodeForge 一键启动脚本 (Windows PowerShell 5.1 / 7.x)
# ----------------------------------------------------------------------------
#  用法(和 start.sh / start.bat 完全一致):
#    .\start.ps1                          默认 (host=0.0.0.0 port=9191 自动开浏览器)
#    .\start.ps1 -Port 8080               自定义端口
#    .\start.ps1 -ListenHost 127.0.0.1    自定义监听地址
#    .\start.ps1 -NoBrowser               不自动开浏览器
#    .\start.ps1 -Rebuild                 强制重建 .venv
#    .\start.ps1 -Update                  只更新 pip 依赖
#    .\start.ps1 -Dev                     开发模式 (Flask debug)
#    .\start.ps1 -Help                    帮助
#
#  首次运行如果报"无法加载脚本",在管理员 PowerShell 执行一次:
#    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
# ============================================================================
[CmdletBinding()]
param(
    [string]$ListenHost = "0.0.0.0",
    [int]$Port          = 9191,
    [switch]$NoBrowser,
    [switch]$Rebuild,
    [switch]$Update,
    [switch]$Dev,
    [switch]$Help
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding         = [System.Text.Encoding]::UTF8

# 切到脚本所在目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ScriptDir

if ($Help) {
    Get-Help $MyInvocation.MyCommand.Path -Detailed | Out-String
    exit 0
}

Write-Host "[start.ps1] platform = windows  (cwd: $ScriptDir)"

# ----------- 找 Python -----------
$py = $null
foreach ($c in @("py -3", "python", "python3")) {
    try {
        $parts = $c -split " "
        $cmd   = $parts[0]
        $arg   = if ($parts.Length -gt 1) { $parts[1..($parts.Length-1)] } else { @() }
        $v = (& $cmd $arg -c "import sys; print(sys.executable)" 2>$null).Trim()
        if ($v) { $py = $c; break }
    } catch {}
}
if (-not $py) {
    Write-Host "[start.ps1] 找不到 Python,请先安装 Python 3.10+" -ForegroundColor Red
    Write-Host "             下载: https://www.python.org/downloads/" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
Write-Host "[start.ps1] python  = $py"

# ----------- venv 路径 -----------
$venvDir   = Join-Path $ScriptDir ".venv"
$activate  = Join-Path $venvDir "Scripts\Activate.ps1"
$depsFlag  = Join-Path $venvDir ".deps_installed"

# ----------- 重建 / 创建 venv -----------
if ($Rebuild -and (Test-Path -LiteralPath $venvDir)) {
    Write-Host "[start.ps1] 重建 venv (--rebuild) ..."
    Remove-Item -LiteralPath $venvDir -Recurse -Force
}
if (-not (Test-Path -LiteralPath $activate)) {
    Write-Host "[start.ps1] 创建 venv ..."
    $parts = $py -split " "
    $cmd   = $parts[0]
    $arg   = if ($parts.Length -gt 1) { $parts[1..($parts.Length-1)] } else { @() }
    & $cmd $arg -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[start.ps1] venv 创建失败" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
}

# ----------- 激活 venv -----------
. $activate

# ----------- 装依赖 -----------
$needInstall = $Update -or -not (Test-Path -LiteralPath $depsFlag)
if ($needInstall) {
    Write-Host "[start.ps1] 安装依赖 ..."
    python -m pip install --upgrade pip wheel --quiet
    $req = $null
    if     (Test-Path -LiteralPath (Join-Path $ScriptDir "requirement.txt"))  { $req = Join-Path $ScriptDir "requirement.txt" }
    elseif (Test-Path -LiteralPath (Join-Path $ScriptDir "requirements.txt")) { $req = Join-Path $ScriptDir "requirements.txt" }
    if ($req) { python -m pip install -r $req --quiet }
    else { Write-Host "[start.ps1] 警告: 找不到 requirement.txt / requirements.txt" -ForegroundColor Yellow }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[start.ps1] pip install 失败" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
    New-Item -ItemType File -Path $depsFlag -Force | Out-Null
}

# ----------- 端口占用检查 -----------
$busy = $false
try {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    if ($conn) { $busy = $true }
} catch {
    # fallback: 试 bind
    try {
        $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $Port)
        $l.Start(); $l.Stop()
    } catch { $busy = $true }
}
if ($busy) {
    Write-Host "[start.ps1] 警告: 端口 $Port 已被占用,试试 -Port" -ForegroundColor Yellow
    $ans = Read-Host "    仍然继续吗? (y/N)"
    if ($ans -notmatch "^[Yy]") { exit 1 }
}

# ----------- 启动主程序 -----------
Write-Host ""
Write-Host "============================================================"
Write-Host "  CodeForge  platform=windows  host=$ListenHost  port=$Port"
Write-Host "  浏览器:    http://localhost:$Port/"
Write-Host "  停止:      Ctrl + C"
Write-Host "============================================================"
Write-Host ""

# 后台开浏览器
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($url)
        Start-Sleep -Seconds 1.5
        try { Start-Process $url } catch {}
    } -ArgumentList "http://localhost:$Port/" | Out-Null
}

# 透传参数
$argsList = @("--host", $ListenHost, "--port", $Port)
if ($Dev) { $argsList += "--debug" }

# ----------- 日志:统一输出到 log/ 目录 -----------
$logDir = Join-Path $ScriptDir "log"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
# 把历史可能落在根目录的 server.log / server.err.log 迁到 log/(首次迁移,不删除,留底)
foreach ($name in @("server.log", "server.err.log")) {
    $old = Join-Path $ScriptDir $name
    $new = Join-Path $logDir   $name
    if ((Test-Path -LiteralPath $old) -and -not (Test-Path -LiteralPath $new)) {
        Move-Item -LiteralPath $old -Destination $new -Force
        Write-Host "[start.ps1] 已迁移 $name -> log/$name"
    }
}
$logOut = Join-Path $logDir "server.log"
$logErr = Join-Path $logDir "server.err.log"
Write-Host "[start.ps1] stdout -> $logOut"
Write-Host "[start.ps1] stderr -> $logErr"

# 用 python 进程跑(接收 Ctrl+C);stdout/stderr 各自重定向到 log/
$proc = Start-Process -FilePath "python" -ArgumentList @(`
    (Join-Path $ScriptDir "main.py") + $argsList `
) -NoNewWindow -PassThru -Wait `
  -RedirectStandardOutput $logOut `
  -RedirectStandardError  $logErr
exit $proc.ExitCode
