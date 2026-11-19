param([ValidateRange(60,86400)][int]$IntervalSeconds = 300)
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path $PSScriptRoot -Parent)
while ($true) {
    docker compose exec -T parser python -m database.collection --enqueue-due
    if ($LASTEXITCODE -ne 0) { Write-Warning "Could not enqueue scheduled collection jobs with exit code $LASTEXITCODE" }
    Start-Sleep -Seconds $IntervalSeconds
}
