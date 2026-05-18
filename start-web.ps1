# start-web.ps1 — Start the Kalshi Trading System web dashboard
# Usage: .\start-web.ps1 [-Env demo|prod] [-Port 8000]
param(
    [string]$Env  = "demo",
    [int]   $Port = 8000
)

Set-Location $PSScriptRoot

# ── Anthropic API key ────────────────────────────────────────────────────────
if (-not $env:ANTHROPIC_API_KEY) {
    $key = Read-Host "Enter your ANTHROPIC_API_KEY (input hidden)" -AsSecureString
    $env:ANTHROPIC_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($key)
    )
}

# ── Kalshi environment ───────────────────────────────────────────────────────
$env:KALSHI_ENV = $Env
Write-Host ""
Write-Host "  Kalshi Trading System — Web Dashboard" -ForegroundColor Cyan
Write-Host "  ──────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "  Environment : $($env:KALSHI_ENV.ToUpper())" -ForegroundColor Yellow
Write-Host "  URL         : http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "  Stop        : Ctrl+C  or  .\stop-web.ps1 -Port $Port" -ForegroundColor DarkGray
Write-Host ""

# ── Check port is free ───────────────────────────────────────────────────────
$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Write-Warning "Port $Port is already in use (PID $($inUse.OwningProcess)). Run .\stop-web.ps1 -Port $Port first."
    exit 1
}

# ── Launch uvicorn ───────────────────────────────────────────────────────────
python -m uvicorn src.web.app:app --host 127.0.0.1 --port $Port --reload
