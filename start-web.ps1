# start-web.ps1 - Start the Kalshi dashboard
# Usage: .\start-web.ps1 [-Env demo|prod] [-Port 8000] [-BindHost 127.0.0.1]
#
# Settings are read from .env in this folder if it exists (KEY=value lines).
# The dashboard listens on 127.0.0.1 by default. For phone access, keep it on
# 127.0.0.1 and publish it with `tailscale serve` (see README), with
# DASHBOARD_PASSWORD set in .env.
param(
    [string]$Env = "",
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1"
)

Set-Location $PSScriptRoot

# Load .env (does not override variables already set in this shell)
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $name = $Matches[1]; $value = $Matches[2].Trim('"').Trim("'")
            if ($value -and -not [Environment]::GetEnvironmentVariable($name, "Process")) {
                [Environment]::SetEnvironmentVariable($name, $value, "Process")
            }
        }
    }
}

if ($Env) { $env:KALSHI_ENV = $Env }
if (-not $env:KALSHI_ENV) { $env:KALSHI_ENV = "demo" }

if (-not $env:ANTHROPIC_API_KEY) {
    $key = Read-Host "Enter your ANTHROPIC_API_KEY (input hidden)" -AsSecureString
    $env:ANTHROPIC_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($key)
    )
}

if ($BindHost -ne "127.0.0.1" -and -not $env:DASHBOARD_PASSWORD) {
    Write-Warning "Binding to $BindHost without DASHBOARD_PASSWORD: remote requests will be refused."
}

# Prefer the py launcher; the bare `python` alias can be the Microsoft Store stub
$python = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

Write-Host ""
Write-Host "  Kalshi Research Center" -ForegroundColor Cyan
Write-Host "  Environment : $($env:KALSHI_ENV.ToUpper())" -ForegroundColor Yellow
Write-Host "  URL         : http://$($BindHost):$Port" -ForegroundColor Green
Write-Host "  Stop        : Ctrl+C  or  .\stop-web.ps1 -Port $Port" -ForegroundColor DarkGray
Write-Host ""

$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Write-Warning "Port $Port is already in use (PID $($inUse.OwningProcess)). Run .\stop-web.ps1 -Port $Port first."
    exit 1
}

& $python -m uvicorn src.web.app:app --host $BindHost --port $Port
