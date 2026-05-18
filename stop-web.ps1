# stop-web.ps1 — Stop the Kalshi Trading System web dashboard
# Usage: .\stop-web.ps1 [-Port 8000]
param(
    [int]$Port = 8000
)

$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue

if (-not $connections) {
    Write-Host "No process found listening on port $Port." -ForegroundColor Yellow
    exit 0
}

foreach ($conn in $connections) {
    $pid = $conn.OwningProcess
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "Stopping '$($proc.Name)' (PID $pid) on port $Port..." -ForegroundColor Cyan
        Stop-Process -Id $pid -Force
        Write-Host "Stopped." -ForegroundColor Green
    } else {
        Write-Warning "Could not find process with PID $pid."
    }
}
