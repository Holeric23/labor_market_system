# Temporary HTTPS access to the Mini App through localhost.run.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$working = Join-Path (Get-Location) '.artifacts\admin-preview'
New-Item -ItemType Directory -Path $working -Force > $null
$knownHosts = Join-Path $working 'known_hosts'
$key = Join-Path $working 'tunnel_key'
$pidFile = Join-Path $working 'ssh.pid'
$stdoutPath = Join-Path $working 'ssh-stdout-current.log'
$stderrPath = Join-Path $working 'ssh-stderr-current.log'
$sshKey = 'AAAAC3NzaC1lZDI1NTE5AAAAILVqOuSMnyeGDVO1lG6EaG5In/dXABCchhmHKkuRU2s9'
[IO.File]::WriteAllText($knownHosts, "localhost.run ssh-ed25519 $sshKey`n", [Text.Encoding]::ASCII)
if (-not (Test-Path -LiteralPath $key)) {
    & ssh-keygen -q -t ed25519 -N '""' -f $key
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать ключ временного туннеля.' }
}
if (Test-Path -LiteralPath $pidFile) {
    $oldId = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$oldId)) {
        $oldProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$oldId" -ErrorAction SilentlyContinue
        if ($oldProcess -and $oldProcess.Name -eq 'ssh.exe' -and $oldProcess.CommandLine -like '*localhost.run*' -and $oldProcess.CommandLine -like '*tunnel_key*') {
            Stop-Process -Id $oldId
        }
    }
}
$arguments = @('-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',"UserKnownHostsFile=$knownHosts",'-o','GlobalKnownHostsFile=NUL','-o','IdentityAgent=none','-o','IdentitiesOnly=yes','-o','PreferredAuthentications=publickey','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3','-i',$key,'-R','80:127.0.0.1:8000','localhost.run','--','--output','json','--inject-http-proxy-headers')
$process = Start-Process -FilePath (Get-Command ssh.exe).Source -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
[IO.File]::WriteAllText($pidFile, [string]$process.Id)
$hostName = $null
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if (Test-Path -LiteralPath $stdoutPath) {
        foreach ($line in (Get-Content -LiteralPath $stdoutPath)) {
            try {
                $event = $line | ConvertFrom-Json
                if ($event.type -eq 'run.localhost.tunnel.registered') { $hostName = $event.subject }
            } catch { }
        }
    }
    if ($hostName) { break }
    if ($process.HasExited) { break }
    Start-Sleep -Seconds 1
}
if ($hostName -notmatch '^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:lhr\.life|localhost\.run)$') { throw 'Не удалось получить допустимый адрес временного туннеля.' }
docker compose up -d database
if ($LASTEXITCODE -ne 0) { throw 'Не удалось запустить базу данных.' }
$db = (docker compose ps -q database).Trim()
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $health = (docker inspect --format '{{.State.Health.Status}}' $db).Trim()
    if ($health -eq 'healthy') { $ready = $true; break }
    if ($health -eq 'unhealthy') { break }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw 'База данных не готова к запуску бота.' }
$gateway = (docker network inspect labor-market-system_default --format '{{(index .IPAM.Config 0).Gateway}}').Trim()
$parsedGateway = $null
if (-not [System.Net.IPAddress]::TryParse($gateway, [ref]$parsedGateway) -or $parsedGateway.Equals([System.Net.IPAddress]::Any)) { throw 'Не удалось определить адрес локального прокси Docker.' }
$oldUrl, $oldHosts, $oldProxy = $env:ADMIN_MINIAPP_URL, $env:ADMIN_ALLOWED_HOSTS, $env:TRUSTED_PROXY_IP
try {
    $env:ADMIN_MINIAPP_URL = "https://$hostName/miniapp"
    $env:ADMIN_ALLOWED_HOSTS = "localhost,127.0.0.1,$hostName"
    $env:TRUSTED_PROXY_IP = $gateway
    docker compose up -d --no-deps bot
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось запустить контейнер бота.' }
    $reachable = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $status = curl.exe --silent --max-time 4 --output NUL --write-out '%{http_code}' $env:ADMIN_MINIAPP_URL
        if ($LASTEXITCODE -eq 0 -and $status -eq '200') { $reachable = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $reachable) { throw 'Туннель подключился, но Mini App по HTTPS не отвечает.' }
    [IO.File]::WriteAllText((Join-Path $working 'current-host.txt'), $hostName)
    $watchPidFile = Join-Path $working 'watcher.pid'
    if (Test-Path -LiteralPath $watchPidFile) {
        $oldWatchId = 0
        if ([int]::TryParse((Get-Content -LiteralPath $watchPidFile -Raw).Trim(), [ref]$oldWatchId)) {
            $oldWatcher = Get-CimInstance Win32_Process -Filter "ProcessId=$oldWatchId" -ErrorAction SilentlyContinue
            if ($oldWatcher -and $oldWatcher.Name -eq 'pwsh.exe' -and $oldWatcher.CommandLine -like '*watch-admin-preview.ps1*') { Stop-Process -Id $oldWatchId }
        }
    }
    $watchScript = Join-Path $PSScriptRoot 'watch-admin-preview.ps1'
    $watcher = Start-Process -FilePath (Get-Command pwsh.exe).Source -ArgumentList @('-NoProfile','-File', $watchScript) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $working 'watcher-stdout.log') -RedirectStandardError (Join-Path $working 'watcher-stderr.log') -PassThru
    [IO.File]::WriteAllText($watchPidFile, [string]$watcher.Id)
    Write-Host "Временный адрес Mini App: https://$hostName/miniapp"
    Write-Host 'Чтобы открыть панель, отправьте боту команду /admin с аккаунта владельца.'
    Write-Host 'Если адрес изменится, отправьте /admin заново. После остановки туннеля запустите скрипт повторно.'
} finally {
    $env:ADMIN_MINIAPP_URL, $env:ADMIN_ALLOWED_HOSTS, $env:TRUSTED_PROXY_IP = $oldUrl, $oldHosts, $oldProxy
}
