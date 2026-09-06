# ============================================================================
#  CodeForge one-click launcher (Windows PowerShell 5.1 / 7.x)
# ----------------------------------------------------------------------------
#  Usage (mirrors start.sh):
#    .\restart.ps1                          default (host=127.0.0.1 port=9191)
#    .\restart.ps1 -Port 8080               custom port
#    .\restart.ps1 -ListenHost 0.0.0.0      listen on every NIC (LAN visible, warns)
#    .\restart.ps1 -NoBrowser               do not auto-open browser
#    .\restart.ps1 -Rebuild                 force-recreate .venv
#    .\restart.ps1 -Update                  refresh pip deps
#    .\restart.ps1 -Dev                     Flask debug mode
#    .\restart.ps1 -Console                 keep server output on the console
#    .\restart.ps1 -Help                    this help
#
#  Auth:
#    The launcher generates CODEFORGE_TOKEN and passes it to main.py, so the
#    auto-opened URL already carries #token=. Export CODEFORGE_TOKEN beforehand
#    to pin it instead.
#
#  Logs:
#    stdout -> log\server.log, stderr -> log\server.err.log (same as start.sh).
#    Use -Console to keep everything on the console instead.
#
#  First-run error "cannot load script"? Run once as admin PowerShell:
#    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
# ============================================================================
[CmdletBinding()]
param(
    [string]$ListenHost = "127.0.0.1",
    [int]$Port          = 9191,
    [switch]$NoBrowser,
    [switch]$Rebuild,
    [switch]$Update,
    [switch]$Dev,
    [switch]$Console,
    [switch]$Help,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Status,
    [string]$Action = ""
)

$ErrorActionPreference = "Stop"

# Switch console + script IO to UTF-8 so any non-ASCII prints don't get garbled
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding          = [System.Text.Encoding]::UTF8
    $PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
} catch {}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ScriptDir

# 鍔ㄤ綔瑙ｆ瀽锛歴top / restart / status / foreground / start(榛樿=restart)
# 涓€涓剼鏈悶瀹氬惎鍔ㄧ鐞嗭細鏃犲弬鏁伴粯璁?restart锛堝厛鍋滄棫鍐嶅悗鍙拌捣锛?
$Mode = if ($Restart) { 'restart' }
        elseif ($Stop)  { 'stop' }
        elseif ($Status) { 'status' }
        elseif ($Console -or $Action -ieq 'foreground' -or $Action -ieq 'console') { 'console' }
        else { 'restart' }
# 浣嶇疆鍙傛暟瑕嗙洊
if ($Action -and $Action -inotin @('foreground','console','start')) {
    switch ($Action.ToLower()) {
        "stop"    { $Mode = 'stop' }
        "restart" { $Mode = 'restart' }
        "status"  { $Mode = 'status' }
        "start"   { $Mode = 'restart' }
    }
}
# --stop/--restart/--status 涔熻鐩?
foreach ($a in $args) {
    switch ($a.ToLower()) {
        "--stop"    { $Mode = 'stop' }
        "--restart" { $Mode = 'restart' }
        "--status"  { $Mode = 'status' }
        "--foreground" { $Mode = 'console' }
    }
}

$pidFile = Join-Path $ScriptDir "log\server.pid"
$logDir  = Join-Path $ScriptDir "log"

function Get-ServerPid {
    # 浼樺厛璇?pid 鏂囦欢
    if (Test-Path -LiteralPath $pidFile) {
        try { $id = [int]((Get-Content -LiteralPath $pidFile -TotalCount 1).Trim()); if ($id -gt 0) { $p = Get-Process -Id $id -ErrorAction SilentlyContinue; if ($p) { return $id } } } catch {}
    }
    # 鍥為€€锛氭寜绔彛鎵?LISTEN 杩涚▼
    try {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { return $c.OwningProcess }
    } catch {}
    return $null
}
function Show-Status {
    $pidFound = Get-ServerPid
    if ($pidFound) {
        try { $p = Get-Process -Id $pidFound -ErrorAction Stop; Write-Host "[restart.ps1] running  pid=$pidFound  port=$Port  cmd=$($p.ProcessName)" -ForegroundColor Green }
        catch { Write-Host "[restart.ps1] pid file points to $pidFound but process not found" -ForegroundColor Yellow }
        try { $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue; if ($conn) { Write-Host "[restart.ps1] port $Port listening" } } catch {}
    } else {
        Write-Host "[restart.ps1] not running  port=$Port" -ForegroundColor Yellow
    }
}
function Stop-Server {
    $pidFound = Get-ServerPid
    if (-not $pidFound) { Write-Host "[restart.ps1] no running service on port $Port" -ForegroundColor Yellow; return }
    Write-Host "[restart.ps1] stopping  pid=$pidFound  port=$Port ..."
    try { Stop-Process -Id $pidFound -Force -ErrorAction Stop; Write-Host "[restart.ps1] stop signal sent" -ForegroundColor Green } catch { Write-Host "[restart.ps1] stop failed: $_" -ForegroundColor Red; return }
    for ($i=0; $i -lt 15; $i++) {
        Start-Sleep -Milliseconds 400
        $still = Get-ServerPid
        if (-not $still) { break }
    }
    if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue }
    $still = Get-ServerPid
    if ($still) { Write-Host "[restart.ps1] process $still still alive, kill manually" -ForegroundColor Red } else { Write-Host "[restart.ps1] stopped" -ForegroundColor Green }
}

if ($Help) {
    Write-Host "Usage: .\restart.ps1 [restart|stop|status|console] [-Port 9191] [-ListenHost 127.0.0.1] [-NoBrowser] [-Dev]"
    Write-Host "  restart   (default) stop old + start in background, print URL, return"
    Write-Host "  stop      stop service on port $Port (via log\server.pid)"
    Write-Host "  status    show running status"
    Write-Host "  console   run in foreground (Ctrl+C to stop)"
    Write-Host "  --foreground / -Console  alias of console"
    Get-Help $MyInvocation.MyCommand.Path -Detailed | Out-String
    exit 0
}
# 闈炲惎鍔ㄧ被鍔ㄤ綔鎻愬墠澶勭悊骞堕€€鍑?
if ($Mode -eq 'status')  { Show-Status; exit 0 }
if ($Mode -eq 'stop')    { Stop-Server; exit 0 }
# console 妯″紡锛氬墠鍙拌繍琛岋紝涓嶅悗鍙板寲锛堝湪鍒濆鍖栧畬鎴愬悗璧帮級
# restart(榛樿) 涓?console锛氱户缁垵濮嬪寲锛宺estart 浼氬湪鍚姩鍓嶅仠鏃?
$isConsole = ($Mode -eq 'console')

Write-Host "[restart.ps1] platform = windows  (cwd: $ScriptDir)"

# ----------- Find Python -----------
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
    Write-Host "[restart.ps1] ERROR: Python 3.10+ not found. Install from https://www.python.org/downloads/" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "[restart.ps1] python  = $py"

# ----------- venv paths -----------
$venvDir   = Join-Path $ScriptDir ".venv"
$activate  = Join-Path $venvDir "Scripts\Activate.ps1"
$depsFlag  = Join-Path $venvDir ".deps_installed"

# ----------- Rebuild / create venv -----------
if ($Rebuild -and (Test-Path -LiteralPath $venvDir)) {
    Write-Host "[restart.ps1] removing existing venv (rebuild) ..."
    Remove-Item -LiteralPath $venvDir -Recurse -Force
}
if (-not (Test-Path -LiteralPath $activate)) {
    Write-Host "[restart.ps1] creating venv ..."
    $parts = $py -split " "
    $cmd   = $parts[0]
    $arg   = if ($parts.Length -gt 1) { $parts[1..($parts.Length-1)] } else { @() }
    & $cmd $arg -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[restart.ps1] ERROR: venv creation failed" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# ----------- Activate venv -----------
. $activate

# ----------- Locate the requirements file -----------
# requirements.txt is the real name; requirement.txt is the old typo, kept so
# that older checkouts still start.
$req = $null
foreach ($name in @("requirements.txt", "requirement.txt")) {
    $candidate = Join-Path $ScriptDir $name
    if (Test-Path -LiteralPath $candidate) { $req = $candidate; break }
}

# ----------- Install dependencies -----------
# The sentinel stores the sha256 of the requirements file rather than being an
# empty marker: edit the list and the hash stops matching, so deps reinstall.
function Get-ReqHash {
    param([string]$Path)
    if (-not $Path) { return "no-requirements" }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

$wantHash = Get-ReqHash -Path $req
$haveHash = ""
if (Test-Path -LiteralPath $depsFlag) {
    try { $haveHash = (Get-Content -LiteralPath $depsFlag -TotalCount 1 -ErrorAction Stop).Trim() } catch { $haveHash = "" }
}

if ($Update -or ($wantHash -ne $haveHash)) {
    if (-not $req) {
        Write-Host "[restart.ps1] WARN: no requirements.txt / requirement.txt" -ForegroundColor Yellow
    } else {
        Write-Host "[restart.ps1] installing dependencies ($(Split-Path -Leaf $req)) ..."
        python -m pip install --upgrade pip wheel --quiet
        python -m pip install -r $req --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[restart.ps1] ERROR: pip install failed" -ForegroundColor Red
            Read-Host "Press Enter to exit"
            exit 1
        }
    }
    Set-Content -LiteralPath $depsFlag -Value $wantHash -Encoding ascii
} else {
    Write-Host "[restart.ps1] dependencies unchanged, skipping install"
}

# ----------- Port-in-use check -----------
$busy = $false
try {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    if ($conn) { $busy = $true }
} catch {
    try {
        $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $Port)
        $l.Start(); $l.Stop()
    } catch { $busy = $true }
}
if ($busy) {
    Write-Host "[restart.ps1] WARN: port $Port already in use. Try -Port." -ForegroundColor Yellow
    $ans = Read-Host "    Continue anyway? (y/N)"
    if ($ans -notmatch "^[Yy]") { exit 1 }
}

# ----------- Auth token -----------
# auth.token() prefers CODEFORGE_TOKEN and only generates one when it is unset,
# so minting it here guarantees the printed URL matches main.py's own banner.
if ([string]::IsNullOrWhiteSpace($env:CODEFORGE_TOKEN)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $env:CODEFORGE_TOKEN = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $tokenSource = "generated for this run"
} else {
    $tokenSource = "environment variable CODEFORGE_TOKEN"
}

# ----------- Reachable URL -----------
# A wildcard bind (0.0.0.0 / ::) is not an address a browser can open; map it to
# loopback exactly like auth.display_host() does so the banner matches reality.
function Get-DisplayHost {
    param([string]$Bind)
    if ([string]::IsNullOrWhiteSpace($Bind) -or $Bind -eq "0.0.0.0" -or $Bind -eq "::") { return "127.0.0.1" }
    if ($Bind.Contains(":")) { return "[$Bind]" }
    return $Bind
}

function Test-Loopback {
    param([string]$Bind)
    return ($Bind -eq "localhost" -or $Bind -eq "::1" -or $Bind -like "127.*")
}

$displayHost = Get-DisplayHost -Bind $ListenHost
$url = "http://${displayHost}:$Port/#token=$($env:CODEFORGE_TOKEN)"

# ----------- Banner -----------
Write-Host ""
Write-Host "============================================================"
Write-Host "  CodeForge  platform=windows  bind=${ListenHost}:$Port"
Write-Host "  Browser:   $url"
Write-Host "  token  :   $($env:CODEFORGE_TOKEN)"
Write-Host "  source :   $tokenSource"
Write-Host "  Stop   :   Ctrl + C"
if (-not (Test-Loopback -Bind $ListenHost)) {
    Write-Host "------------------------------------------------------------"
    Write-Host "  WARNING: bound to non-loopback $ListenHost - every device on the LAN can reach this service." -ForegroundColor Yellow
    Write-Host "  WARNING: the token is the only defence left; leaking it hands over a shell on this machine." -ForegroundColor Yellow
    Write-Host "  WARNING: only do this on a trusted network, otherwise keep the default -ListenHost 127.0.0.1." -ForegroundColor Yellow
}
Write-Host "============================================================"
Write-Host ""

# ----------- Open browser in background -----------
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($target)
        Start-Sleep -Seconds 1.5
        try { Start-Process $target } catch {}
    } -ArgumentList $url | Out-Null
}

# ----------- Logs: everything under log\, same as start.sh -----------
# Windows redirection cannot append the way start.sh does, so the previous run
# is rotated to server.prev.log / server.err.prev.log instead of being dropped.
# $logDir 宸插湪椤堕儴瀹氫箟锛堝 stop/status 宸茬敤锛夛紝杩欓噷澶嶇敤
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

foreach ($name in @("server.log", "server.err.log")) {
    $stray = Join-Path $ScriptDir $name
    $dest  = Join-Path $logDir $name
    if ((Test-Path -LiteralPath $stray) -and -not (Test-Path -LiteralPath $dest)) {
        Move-Item -LiteralPath $stray -Destination $dest
        Write-Host "[restart.ps1] moved $name -> log\$name"
    }
}

$logOut = Join-Path $logDir "server.log"
$logErr = Join-Path $logDir "server.err.log"

# ----------- Run main.py -----------
# main.py reads --host / --port / --debug; also honors CODEFORGE_HOST / _PORT / _DEBUG env
$pyFile = Join-Path $ScriptDir "main.py"
$pyArgs = @("--host", $ListenHost, "--port", $Port)
if ($Dev) { $pyArgs += "--debug" }

if ($isConsole) {
    Write-Host "[restart.ps1] foreground, Ctrl+C to stop ..."
    & python $pyFile $pyArgs
    exit $LASTEXITCODE
}

# ===== restart锛堥粯璁わ級锛氬厛鍋滄棫锛屽啀鍚庡彴鍚姩锛屾墦鍗板湴鍧€锛岀珛鍗宠繑鍥?=====
if (-not $isConsole) {
    # 鍋滄帀鏃у疄渚嬶紙鏃犲垯璺宠繃锛夆€斺€斿繀椤诲湪鏃嬭浆鏃ュ織涔嬪墠锛屽惁鍒欐棫杩涚▼杩樺崰鐫€ server.log 鍙ユ焺
    $was = Get-ServerPid
    if ($was) {
        Write-Host "[restart.ps1] stop old (pid=$was) ..."
        try { Stop-Process -Id $was -Force -ErrorAction Stop } catch {}
        Start-Sleep -Milliseconds 600
        if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue }
    }
    # 鏃ц繘绋嬪凡姝伙紝鏃嬭浆鏃ュ織鎵嶅畨鍏?
    foreach ($pair in @(@($logOut, "server.prev.log"), @($logErr, "server.err.prev.log"))) {
        if (Test-Path -LiteralPath $pair[0]) {
            try { Move-Item -LiteralPath $pair[0] -Destination (Join-Path $logDir $pair[1]) -Force -ErrorAction Stop } catch {}
        }
    }
}

Write-Host "[restart.ps1] stdout -> $logOut"
Write-Host "[restart.ps1] stderr -> $logErr"
Write-Host "[restart.ps1] starting in background ..."

$pyExe = (Get-Command python).Source
$proc  = Start-Process -FilePath $pyExe -ArgumentList (@($pyFile) + $pyArgs) `
                       -NoNewWindow -PassThru `
                       -RedirectStandardOutput $logOut -RedirectStandardError $logErr
try {
    Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ascii
    # 缁欐湇鍔?3 绉掔‘璁ゅ凡璧?
    Start-Sleep -Seconds 3
    $alive = Get-ServerPid
    if ($alive) {
        Write-Host "[restart.ps1] running  pid=$($proc.Id)  port=$Port"
        Write-Host "[restart.ps1] Browser: $url"
        Write-Host "[restart.ps1] token  : $($env:CODEFORGE_TOKEN)"
        Write-Host "[restart.ps1] done. manage with: .\restart.ps1 stop | status | restart"
        exit 0
    } else {
        Write-Host "[restart.ps1] WARN: process started but not listening yet. check log\server.err.log" -ForegroundColor Yellow
        Write-Host "============================================================"
        Write-Host "  Browser:   $url"
        Write-Host "  token  :   $($env:CODEFORGE_TOKEN)"
        Write-Host "============================================================"
        exit 0
    }
} finally {
    if (Test-Path -LiteralPath $pidFile) {
        try {
            $saved = (Get-Content -LiteralPath $pidFile -TotalCount 1 -ErrorAction SilentlyContinue).Trim()
            if ($saved -eq "$($proc.Id)") {
                # 鏈嶅姟浠嶅湪璺戞墠淇濈暀 pid 鏂囦欢锛涘凡閫€鍑哄垯娓呮帀
                if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
                    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
                }
            }
        } catch {}
    }
}