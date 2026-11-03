# ============================================================================
# run_local.ps1 - Run the manager on this PC with the UNICT VPN, t2g only.
#
# Usage (from the manager folder):
#     .\run_local.ps1                    # VPN up, manager on :8765, manual ticks
#     .\run_local.ps1 -TickMinutes 30    # + POST /tick every 30 min while running
#     .\run_local.ps1 -NoVpn             # already on the university network / VPN
#
# Point the TUI at it: MANAGER_URL=http://127.0.0.1:8765 (.\run_manager_tui.ps1 -Url ...).
# Stop: Ctrl+C -> stops the manager and the tick job, then disconnects the VPN.
# ============================================================================
param(
    [int]$Port = 8765,
    [int]$TickMinutes = 0,
    [switch]$NoVpn
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
$proc = $null
$tickJob = $null
$vpnUp = $false
try {
    $envFile = Join-Path $PSScriptRoot ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            $t = $line.Trim()
            if ($t -and -not $t.StartsWith("#")) {
                $k, $v = $t -split '=', 2
                if ($k -and $null -ne $v) { Set-Item -Path "Env:$($k.Trim())" -Value $v.Trim().Trim('"') }
            }
        }
    }
    $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { throw ".venv missing: run 'uv sync' first" }
    if (-not $Env:MANAGER_AUTH_TOKEN) { throw "MANAGER_AUTH_TOKEN missing in .env" }

    if (-not $NoVpn) {
        & "$PSScriptRoot\vpn.ps1" -Action connect
        $vpnUp = $true
        & "$PSScriptRoot\vpn.ps1" -Action test   # fail early if the cluster is unreachable
    }

    # Only t2g: the committer must keep running on Render only (no double commits).
    # No restart on script updates: nobody would relaunch the process here.
    # Set AFTER vpn.ps1: it reloads .env into the same process and would overwrite these.
    $Env:SERVICES_ENABLED = "t2g"
    $Env:RESTART_ON_CHANGE = "0"

    $proc = Start-Process $python -ArgumentList "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", $Port `
        -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru
    $base = "http://127.0.0.1:$Port"   # uvicorn binds IPv4 only: "localhost" may try ::1 first
    $up = $false
    for ($i = 0; $i -lt 60 -and -not $up -and -not $proc.HasExited; $i++) {
        try { $up = (Invoke-RestMethod "$base/health" -TimeoutSec 2).status -eq "ok" } catch { Start-Sleep 1 }
    }
    if (-not $up) { throw "manager did not come up on $base" }
    Write-Host "manager up on $base (t2g only). Ctrl+C to stop." -ForegroundColor Green

    if ($TickMinutes -gt 0) {
        $tickJob = Start-Job -ArgumentList $base, $Env:MANAGER_AUTH_TOKEN, $TickMinutes -ScriptBlock {
            param($base, $token, $minutes)
            while ($true) {
                try {
                    $r = Invoke-RestMethod -Method Post "$base/tick" -Headers @{ "X-Auth-Token" = $token } -TimeoutSec 120
                    "{0:HH:mm:ss} tick ok={1}" -f (Get-Date), $r.ok
                }
                catch { "{0:HH:mm:ss} tick failed: {1}" -f (Get-Date), $_.Exception.Message }
                Start-Sleep -Seconds ($minutes * 60)
            }
        }
        Write-Host "ticking every $TickMinutes min" -ForegroundColor Green
    }

    while (-not $proc.HasExited) {
        if ($tickJob) { Receive-Job $tickJob | ForEach-Object { Write-Host $_ } }
        Start-Sleep -Seconds 1
    }
    Write-Host "manager exited (code $($proc.ExitCode))" -ForegroundColor Yellow
}
finally {
    if ($tickJob) { Stop-Job $tickJob -ErrorAction SilentlyContinue; Remove-Job $tickJob -Force -ErrorAction SilentlyContinue }
    # /T: the venv python.exe is a launcher that spawns the real interpreter as a child
    if ($proc -and -not $proc.HasExited) { taskkill /PID $proc.Id /T /F | Out-Null }
    if ($vpnUp) { & "$PSScriptRoot\vpn.ps1" -Action disconnect }
    Pop-Location
}
