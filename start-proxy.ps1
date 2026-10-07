$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$LogRequestBodies = $args -contains "-LogRequestBodies"

$env:PROXY_HOST = "127.0.0.1"
$env:PROXY_PORT = "8080"
$env:UPSTREAM_BASE = "http://127.0.0.1:8081"
$env:PROXY_LOG_DIR = Join-Path $PSScriptRoot "logs"
$env:LOG_REQUEST_BODIES = if ($LogRequestBodies) { "1" } else { "0" }
$env:CANCEL_ON_DISCONNECT = "1"
$env:CANCEL_TIMEOUT = "5"
$env:READ_TIMEOUT = "none"

Write-Host "======================================" -ForegroundColor Cyan
Write-Host " VS Code -> llama.cpp Proxy" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "VS Code  : http://127.0.0.1:8080"
Write-Host "llama.cpp: http://127.0.0.1:8081"
Write-Host "Logs     : $env:PROXY_LOG_DIR"
Write-Host "Body log : $env:LOG_REQUEST_BODIES (use -LogRequestBodies to enable)"
Write-Host ""
Write-Host "Ctrl+C stops the proxy." -ForegroundColor DarkGray
Write-Host ""

python .\proxy.py
