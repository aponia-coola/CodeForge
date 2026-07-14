# ============================================================================
#  CodeForge one-click launcher (Windows PowerShell 5.1 / 7.x)
# ----------------------------------------------------------------------------
#  Usage (mirrors start.sh / start.bat):
#    .\start.ps1                          default (host=0.0.0.0 port=9191)
#    .\start.ps1 -Port 8080               custom port
#    .\start.ps1 -ListenHost 127.0.0.1    custom listen address
#    .\start.ps1 -NoBrowser               do not auto-open browser
#    .\start.ps1 -Rebuild                 force-recreate .venv
#    .\start.ps1 -Update                  refresh pip deps
#    .\start.ps1 -Dev                     Flask debug mode
#    .\start.ps1 -Help                    this help
#
#  First-run error "cannot load script"? Run once as admin PowerShell:
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

# Switch console + script IO to UTF-8 so any non-ASCII prints don't get garbled
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding          = [System.Text.Encoding]::UTF8
    $PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
} catch {}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ScriptDir

if ($Help) {
    Get-Help $MyInvocation.MyCommand.Path -Detailed | Out-String
    exit 0
}

Write-Host "[start.ps1] platform = windows  (cwd: $ScriptDir)"

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
    Write-Host "[start.ps1] ERROR: Python 3.10+ not found. Install from https://www.python.org/downloads/" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "[start.ps1] python  = $py"

# ----------- venv paths -----------
$venvDir   = Join-Path $ScriptDir ".venv"
$activate  = Join-Path $venvDir "Scripts\Activate.ps1"
$depsFlag  = Join-Path $venvDir ".deps_installed"

# ----------- Rebuild / create venv -----------
if ($Rebuild -and (Test-Path -LiteralPath $venvDir)) {
    Write-Host "[start.ps1] removing existing venv (rebuild) ..."
    Remove-Item -LiteralPath $venvDir -Recurse -Force
}
if (-not (Test-Path -LiteralPath $activate)) {
    Write-Host "[start.ps1] creating venv ..."
    $parts = $py -split " "
    $cmd   = $parts[0]
    $arg   = if ($parts.Length -gt 1) { $parts[1..($parts.Length-1)] } else { @() }
    & $cmd $arg -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[start.ps1] ERROR: venv creation failed" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# ----------- Activate venv -----------
. $activate

# ----------- Install dependencies -----------
$needInstall = $Update -or -not (Test-Path -LiteralPath $depsFlag)
if ($needInstall) {
    Write-Host "[start.ps1] installing dependencies ..."
    python -m pip install --upgrade pip wheel --quiet
    $req = $null
    if     (Test-Path -LiteralPath (Join-Path $ScriptDir "requirement.txt"))  { $req = Join-Path $ScriptDir "requirement.txt" }
    elseif (Test-Path -LiteralPath (Join-Path $ScriptDir "requirements.txt")) { $req = Join-Path $ScriptDir "requirements.txt" }
    if ($req) { python -m pip install -r $req --quiet }
    else { Write-Host "[start.ps1] WARN: no requirement.txt / requirements.txt" -ForegroundColor Yellow }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[start.ps1] ERROR: pip install failed" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    New-Item -ItemType File -Path $depsFlag -Force | Out-Null
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
    Write-Host "[start.ps1] WARN: port $Port already in use. Try -Port." -ForegroundColor Yellow
    $ans = Read-Host "    Continue anyway? (y/N)"
    if ($ans -notmatch "^[Yy]") { exit 1 }
}

# ----------- Banner -----------
Write-Host ""
Write-Host "============================================================"
Write-Host "  CodeForge  platform=windows  host=$ListenHost  port=$Port"
Write-Host "  Browser:   http://localhost:$Port/"
Write-Host "  Stop:      Ctrl + C"
Write-Host "============================================================"
Write-Host ""

# ----------- Open browser in background -----------
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($url)
        Start-Sleep -Seconds 1.5
        try { Start-Process $url } catch {}
    } -ArgumentList "http://localhost:$Port/" | Out-Null
}

# ----------- Run main.py directly (no Start-Process, keeps Ctrl+C working) -----------
# main.py reads --host / --port / --debug; also honors CODEFORGE_HOST / _PORT / _DEBUG env
$pyFile = Join-Path $ScriptDir "main.py"
$pyArgs = @("--host", $ListenHost, "--port", $Port)
if ($Dev) { $pyArgs += "--debug" }

& python $pyFile $pyArgs
exit $LASTEXITCODE