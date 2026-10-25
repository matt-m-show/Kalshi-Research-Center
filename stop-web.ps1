# stop-web.ps1 - Stop the Kalshi dashboard
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
    $procId = $conn.OwningProcess
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "Stopping '$($proc.Name)' (PID $procId) on port $Port..." -ForegroundColor Cyan
        Stop-Process -Id $procId -Force
        Write-Host "Stopped." -ForegroundColor Green
    } else {
        Write-Warning "Could not find process with PID $procId."
    }
}
