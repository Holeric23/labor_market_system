# Keep the bot configured for the latest hostname assigned to the active temporary tunnel.
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$working = Join-Path (Get-Location) '.artifacts\admin-preview'
$eventsFile = Join-Path $working 'ssh-stdout-current.log'
$hostFile = Join-Path $working 'current-host.txt'
$tunnelPidFile = Join-Path $working 'ssh.pid'
while ($true) {
    try {
        $tunnelId = 0
        if (-not (Test-Path -LiteralPath $tunnelPidFile) -or -not [int]::TryParse((Get-Content -LiteralPath $tunnelPidFile -Raw).Trim(), [ref]$tunnelId)) { break }
        $tunnel = Get-CimInstance Win32_Process -Filter "ProcessId=$tunnelId" -ErrorAction SilentlyContinue
        if (-not $tunnel -or $tunnel.Name -ne 'ssh.exe' -or $tunnel.CommandLine -notlike '*localhost.run*' -or $tunnel.CommandLine -notlike '*tunnel_key*') { break }
        $latest = $null
        if (Test-Path -LiteralPath $eventsFile) {
            foreach ($line in (Get-Content -LiteralPath $eventsFile -Tail 20)) {
                try {
                    $event = $line | ConvertFrom-Json
                    if ($event.type -eq 'run.localhost.tunnel.registered') { $latest = $event.subject }
                    elseif ($event.type -eq 'run.localhost.tunnel.removed' -and $latest -eq $event.subject) { $latest = $null }
                } catch { }
            }
        }
        if ($latest -and $latest -match '^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:lhr\.life|localhost\.run)$') {
            $previous = if (Test-Path -LiteralPath $hostFile) { (Get-Content -LiteralPath $hostFile -Raw).Trim() } else { '' }
            if ($latest -ne $previous) {
                $gateway = (docker network inspect labor-market-system_default --format '{{(index .IPAM.Config 0).Gateway}}').Trim()
                $parsed = $null
                if ([System.Net.IPAddress]::TryParse($gateway,[ref]$parsed) -and -not $parsed.Equals([System.Net.IPAddress]::Any)) {
                    $env:ADMIN_MINIAPP_URL = "https://$latest/miniapp"
                    $env:ADMIN_ALLOWED_HOSTS = "localhost,127.0.0.1,$latest"
                    $env:TRUSTED_PROXY_IP = $gateway
                    docker compose up -d --no-deps bot
                    if ($LASTEXITCODE -eq 0) {
                        [IO.File]::WriteAllText($hostFile,$latest)
                        Write-Output "Bot configured for $latest"
                    }
                }
            }
        }
    } catch { Write-Output 'Could not update temporary address; retrying.' }
    Start-Sleep -Seconds 5
}
