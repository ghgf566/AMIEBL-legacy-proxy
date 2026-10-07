param(
    [switch]$Stop,
    [switch]$LogRequestBodies
)

$ErrorActionPreference = "Stop"

# ==========================================
# ⚙️ 基本路徑設定 (可依需求修改)
# ==========================================
$Root = "C:\Users\kissi\llama.cpp"
$ModelBaseDir = "D:\model"  # 腳本會自動掃描這個資料夾底下的所有模型
$FitTargetMiB = 2048
$Port = 8081
$ProxyPort = 8080
$HostAddress = "127.0.0.1"
$ProxyDir = Join-Path $Root "llama-vscode-proxy"
$ProxyScript = Join-Path $ProxyDir "proxy.py"
$ProxyPidFile = Join-Path $Root ".llama-proxy.pid"
$ServerPidFile = Join-Path $Root ".llama-server.pid"
# ==========================================

$FitExe = Join-Path $Root "llama-fit-params.exe"
$ServerExe = Join-Path $Root "llama-server.exe"

if ($Stop) {
    Write-Host "正在停止 Local LLM..." -ForegroundColor Yellow

    foreach ($entry in @(
        @{ Name = "Proxy"; PidFile = $ProxyPidFile },
        @{ Name = "llama-server"; PidFile = $ServerPidFile }
    )) {
        if (Test-Path $entry.PidFile) {
            try {
                $pidValue = [int](Get-Content $entry.PidFile -Raw)
                $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "正在停止 $($entry.Name)（PID $pidValue）..." -ForegroundColor Yellow
                    Stop-Process -Id $pidValue -Force
                    Write-Host ">> $($entry.Name) 已停止。" -ForegroundColor Green
                }
            } catch {
                Write-Host ">> $($entry.Name) 停止失敗：$($_.Exception.Message)" -ForegroundColor DarkYellow
            }
            Remove-Item $entry.PidFile -Force -ErrorAction SilentlyContinue
        }
    }

    # 相容舊啟動狀態：若沒有 PID 檔，仍嘗試停止此 Root 下的 llama-server。
    $servers = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $ServerExe }
    foreach ($server in $servers) {
        Write-Host "正在停止 llama-server（PID $($server.Id)）..." -ForegroundColor Yellow
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }

    # PID 檔可能因視窗異常關閉而遺失；以完整命令列辨識本專案的 proxy，
    # 避免誤殺其他 Python 程序。
    $proxyProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($ProxyScript) }
    foreach ($proxy in $proxyProcesses) {
        Write-Host "正在停止 Proxy（PID $($proxy.ProcessId)）..." -ForegroundColor Yellow
        Stop-Process -Id $proxy.ProcessId -Force -ErrorAction SilentlyContinue
    }

    Write-Host "Local LLM 已停止。" -ForegroundColor Cyan
    exit 0
}

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "       Llama.cpp 互動式啟動器       " -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan

$EnableProxyBodyLog = [bool]$LogRequestBodies
if (-not $LogRequestBodies) {
    $BodyLogInput = Read-Host "是否記錄完整 VS Code request body？可能包含原始碼與私人資料，僅建議除錯時開啟 [y/N]"
    $EnableProxyBodyLog = $BodyLogInput -match '^(y|yes)$'
}

# ------------------------------------------
# 1. 選擇 Context 大小
# ------------------------------------------
Write-Host "`n[1] 選擇 Context 大小:" -ForegroundColor Yellow
$ctxOptions = [ordered]@{
    '1' = 4096
    '2' = 8192
    '3' = 16384
    '4' = 32768
    '5' = 65536
    '6' = 131072
    '7' = 262144
}
foreach ($key in $ctxOptions.Keys) {
    Write-Host "  $key) $($ctxOptions[$key])"
}
$ctxInput = Read-Host "請輸入對應數字 (預設為 5: 65536)"
if ([string]::IsNullOrWhiteSpace($ctxInput) -or -not $ctxOptions.Contains($ctxInput)) {
    $ctxInput = '5'
}
$Context = $ctxOptions[$ctxInput]
Write-Host ">> 已選擇 Context: $Context`n" -ForegroundColor Green

# ------------------------------------------
# 2. 自動偵測與選擇主模型 (排除 mmproj)
# ------------------------------------------
Write-Host "[2] 掃描可用模型中 ($ModelBaseDir)..." -ForegroundColor Yellow
$allGguf = Get-ChildItem -Path $ModelBaseDir -Filter "*.gguf" -Recurse -File
$mainModels = $allGguf | Where-Object { $_.Name -notmatch "mmproj" }

if ($mainModels.Count -eq 0) {
    throw "在 $ModelBaseDir 找不到任何模型！"
}

for ($i = 0; $i -lt $mainModels.Count; $i++) {
    Write-Host "  $($i + 1)) $($mainModels[$i].Name)" -ForegroundColor White
}
$modInput = Read-Host "請選擇要載入的模型 (輸入數字，預設為 1)"
$modIndex = [int]$modInput - 1
if ($modIndex -lt 0 -or $modIndex -ge $mainModels.Count) { $modIndex = 0 }
$SelectedModel = $mainModels[$modIndex].FullName
Write-Host ">> 已選擇模型: $($mainModels[$modIndex].Name)`n" -ForegroundColor Green

# ------------------------------------------
# 3. 多模態 (Vision) 模組選擇
# ------------------------------------------
Write-Host "[3] 是否需要載入多模態 (Vision) 模型？(y/n)" -ForegroundColor Yellow
$visionInput = Read-Host "請選擇 (預設為 n)"
$SelectedMmproj = $null

if ($visionInput -match "^[yY]") {
    $visionModels = $allGguf | Where-Object { $_.Name -match "mmproj" }
    if ($visionModels.Count -gt 0) {
        for ($i = 0; $i -lt $visionModels.Count; $i++) {
            Write-Host "  $($i + 1)) $($visionModels[$i].Name)"
        }
        $vModInput = Read-Host "請選擇 Vision 模型 (輸入數字，預設為 1)"
        $vModIndex = [int]$vModInput - 1
        if ($vModIndex -lt 0 -or $vModIndex -ge $visionModels.Count) { $vModIndex = 0 }
        $SelectedMmproj = $visionModels[$vModIndex].FullName
        Write-Host ">> 已選擇 Vision: $($visionModels[$vModIndex].Name)`n" -ForegroundColor Green
    } else {
        Write-Host ">> 找不到任何名稱包含 mmproj 的多模態模型，已略過。`n" -ForegroundColor Red
    }
} else {
    Write-Host ">> 不載入多模態模型。`n" -ForegroundColor DarkGray
}

# ------------------------------------------
# 4. 計算 GPU 資源 (llama-fit-params)
# ------------------------------------------
Write-Host "[4/6] 正在計算 GPU 層數分配..." -ForegroundColor Cyan
$FitArgs = @(
    "-m", $SelectedModel,
    "-c", $Context,
    "-ctk", "q4_0",
    "-ctv", "q4_0",
    "--fit-target", $FitTargetMiB
)

$OriginalErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$FitOutput = @(& $FitExe @FitArgs 2>&1 | ForEach-Object { $_.ToString() })
$ErrorActionPreference =$OriginalErrorAction

if ($LASTEXITCODE -ne 0) {
    throw "llama-fit-params.exe 執行失敗。`n$($FitOutput -join "`n")"
}

$FitText =$FitOutput -join "`n"
$NglMatch = [regex]::Match($FitText, '(?m)-ngl\s+(\d+)')
if (-not $NglMatch.Success) { throw "無法解析 -ngl 數值。" }
$Ngl = [int]$NglMatch.Groups[1].Value

Write-Host ">> 預估結果: Context $Context, GPU Layers $Ngl`n" -ForegroundColor Green

# ------------------------------------------
# 5. 建立 llama-server 啟動參數
# ------------------------------------------
$ServerArgs = @(

    "-m", $SelectedModel,
    "--alias", "qwen3.8-27b-local",

    "-c", $Context,
    "-ctk", "q4_0",
    "-ctv", "q4_0",

    "--spec-type", "draft-mtp",
    "--spec-draft-n-max", "2",

    "--fit", "off",
    "-ngl", $Ngl,

    "-t", "-1",
    "-tb", "-1",

    "--jinja",

    # Per-request effort/budget is supplied by the proxy. -1 is required or
    # llama-server will ignore thinking_budget_tokens from the request body.
    "--reasoning-effort", "default",
    "--reasoning-budget", "-1",
    "--no-reasoning-preserve",

    "--timeout", "18000",
    "--sse-ping-interval", "10",
    "--host", $HostAddress,

    "--port", $Port,
    "-np", "1"
)

if ($SelectedMmproj) {
    $ServerArgs += @("--mmproj", $SelectedMmproj, "--image-min-tokens", "1024")
}

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "伺服器位址 : http://${HostAddress}:${Port}"
Write-Host "模型檔案   : $(Split-Path $SelectedModel -Leaf)"
Write-Host "多模態檔案 : $(if ($SelectedMmproj) { Split-Path $SelectedMmproj -Leaf } else { '無' })"
Write-Host "Context    : $Context"
Write-Host "GPU 層數   : $Ngl"
Write-Host "======================================" -ForegroundColor Cyan
# ------------------------------------------
# 6. 啟動 llama-server + VS Code Proxy
# ------------------------------------------
if (-not (Test-Path $ProxyScript)) {
    throw "找不到 Proxy：$ProxyScript"
}

$RequirementsFile = Join-Path $ProxyDir "requirements.txt"
$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    throw "找不到 Python。請確認 python.exe 已加入 PATH。"
}

# 一鍵啟動：若 Proxy 相依套件還沒裝，自動安裝一次。
$OriginalErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $PythonCmd.Source -c "import fastapi, httpx, uvicorn" 2>$null
$ProxyDepsOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $OriginalErrorAction

if (-not $ProxyDepsOk) {
    if (-not (Test-Path $RequirementsFile)) {
        throw "Proxy 缺少 Python 相依套件，而且找不到 requirements.txt：$RequirementsFile"
    }

    Write-Host ">> 第一次使用 Proxy，正在安裝 Python 相依套件..." -ForegroundColor Yellow
    & $PythonCmd.Source -m pip install -r $RequirementsFile
    if ($LASTEXITCODE -ne 0) {
        throw "Proxy Python 相依套件安裝失敗。"
    }
}

# 若上次異常結束留下 PID 檔，先清掉對應程序。
foreach ($entry in @(
    @{ Name = "Proxy"; PidFile = $ProxyPidFile },
    @{ Name = "llama-server"; PidFile = $ServerPidFile }
)) {
    if (Test-Path $entry.PidFile) {
        try {
            $oldPid = [int](Get-Content $entry.PidFile -Raw)
            $oldProc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
            if ($oldProc) {
                Write-Host ">> 發現舊 $($entry.Name)（PID $oldPid），先停止。" -ForegroundColor DarkYellow
                Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 300
            }
        } catch {}
        Remove-Item $entry.PidFile -Force -ErrorAction SilentlyContinue
    }
}

$ProxyProcess = $null
$ServerProcess = $null

try {
    # 先啟動 llama-server，避免 Proxy 已經開始接請求但 upstream 還沒 ready。
    Write-Host "[5/6] 啟動 llama-server..." -ForegroundColor Yellow
    $ServerProcess = Start-Process `
        -FilePath $ServerExe `
        -ArgumentList $ServerArgs `
        -WorkingDirectory $Root `
        -NoNewWindow `
        -PassThru

    $ServerProcess.Id | Set-Content $ServerPidFile
    Write-Host ">> llama-server PID : $($ServerProcess.Id)" -ForegroundColor Green

    # 等待 llama-server 真正完成模型載入。
    $ServerReady = $false
    for ($i = 0; $i -lt 720; $i++) {
        if ($ServerProcess.HasExited) {
            throw "llama-server 在完成啟動前就已結束（ExitCode=$($ServerProcess.ExitCode)）。"
        }

        try {
            $response = Invoke-WebRequest `
                -Uri "http://${HostAddress}:${Port}/health" `
                -UseBasicParsing `
                -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                $ServerReady = $true
                break
            }
        } catch {}

        Start-Sleep -Milliseconds 250
    }

    if (-not $ServerReady) {
        throw "llama-server 啟動逾時，無法連線到 http://${HostAddress}:${Port}/health"
    }

    Write-Host ">> llama-server 已就緒 : http://${HostAddress}:${Port}" -ForegroundColor Green

    # 明確指定 Proxy 環境，避免未來修改 proxy.py 預設值後發生埠號錯配。
    $env:PROXY_HOST = $HostAddress
    $env:PROXY_PORT = "$ProxyPort"
    $env:UPSTREAM_BASE = "http://${HostAddress}:${Port}"
    $env:PROXY_LOG_DIR = Join-Path $ProxyDir "logs"
    if ($EnableProxyBodyLog) {
        $env:LOG_REQUEST_BODIES = "1"
    } else {
        $env:LOG_REQUEST_BODIES = "0"
    }
    $env:CANCEL_ON_DISCONNECT = "1"
    $env:CANCEL_TIMEOUT = "5"
    $env:READ_TIMEOUT = "none"

    Write-Host "[6/6] 啟動 VS Code Proxy..." -ForegroundColor Yellow
    $ProxyProcess = Start-Process `
        -FilePath $PythonCmd.Source `
        -ArgumentList "`"$ProxyScript`"" `
        -WorkingDirectory $ProxyDir `
        -NoNewWindow `
        -PassThru

    $ProxyProcess.Id | Set-Content $ProxyPidFile

    # 等待 Proxy health endpoint。
    $ProxyReady = $false
    for ($i = 0; $i -lt 40; $i++) {
        if ($ProxyProcess.HasExited) {
            throw "Proxy 在完成啟動前就已結束（ExitCode=$($ProxyProcess.ExitCode)）。"
        }

        try {
            $response = Invoke-WebRequest `
                -Uri "http://${HostAddress}:${ProxyPort}/health" `
                -UseBasicParsing `
                -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                $ProxyReady = $true
                break
            }
        } catch {}

        Start-Sleep -Milliseconds 250
    }

    if (-not $ProxyReady) {
        throw "Proxy 啟動失敗，無法連線到 http://${HostAddress}:${ProxyPort}/health"
    }

    Write-Host ""
    Write-Host "======================================" -ForegroundColor Cyan
    Write-Host "VS Code API  : http://${HostAddress}:${ProxyPort}/v1/chat/completions"
    Write-Host "Proxy        : http://${HostAddress}:${ProxyPort}"
    Write-Host "llama-server : http://${HostAddress}:${Port}"
    Write-Host "Proxy Logs   : $env:PROXY_LOG_DIR"
    Write-Host "模型檔案     : $(Split-Path $SelectedModel -Leaf)"
    Write-Host "Context      : $Context"
    Write-Host "GPU 層數      : $Ngl"
    Write-Host "======================================" -ForegroundColor Cyan
    Write-Host "在這個 PowerShell 視窗按下 Ctrl+C，可停止 llama-server 與 Proxy。" -ForegroundColor DarkGray
    Write-Host "也可執行「一鍵啟動.bat stop」正常停止服務。" -ForegroundColor DarkGray
    Write-Host ""

    Wait-Process -Id $ServerProcess.Id
}
finally {
    Write-Host "`n正在清理 Local LLM..." -ForegroundColor Yellow

    if ($ProxyProcess -and -not $ProxyProcess.HasExited) {
        Stop-Process -Id $ProxyProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $ProxyPidFile -Force -ErrorAction SilentlyContinue

    if ($ServerProcess -and -not $ServerProcess.HasExited) {
        Stop-Process -Id $ServerProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $ServerPidFile -Force -ErrorAction SilentlyContinue

    Write-Host ">> llama-server 與 Proxy 已停止。" -ForegroundColor Green
}
