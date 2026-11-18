param([ValidateRange(60,86400)][int]$IntervalSeconds = 300)
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path $PSScriptRoot -Parent)
while ($true) {
    docker compose --profile parsing run --rm parser --due
    if ($LASTEXITCODE -ne 0) { Write-Warning "Parser run failed with exit code $LASTEXITCODE" }
    Start-Sleep -Seconds $IntervalSeconds
}
