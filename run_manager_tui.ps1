# ============================================================================
# run_manager_tui.ps1 - Launch the render-service-manager TUI dashboard.
#
# Usage (from the manager folder):
#     .\run_manager_tui.ps1
#     .\run_manager_tui.ps1 -Url https://your-manager-url
#
# Reads URL and tokens from .env (MANAGER_URL, MANAGER_AUTH_TOKEN, T2G_AUTH_TOKEN)
# or from CLI flags. Stop: Ctrl+C or 'q'.
# ============================================================================
param(
    [string]$Url,
    [string]$Token,
    [string]$T2gToken
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    # -- Load .env --
    $envFile = Join-Path $PSScriptRoot ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            $t = $line.Trim()
            if ($t -and -not $t.StartsWith("#")) {
                $name, $value = $t -split '=', 2
                if ($name -and $null -ne $value) {
                    Set-Item -Path "Env:$($name.Trim())" -Value $value.Trim().Trim('"')
                }
            }
        }
    }

    # -- Resolve params (CLI > env) --
    if (-not $Url) { $Url = $Env:MANAGER_URL }
    if (-not $Token) { $Token = $Env:MANAGER_AUTH_TOKEN }
    if (-not $T2gToken) { $T2gToken = $Env:T2G_AUTH_TOKEN }

    # -- Require a manager URL (no hardcoded fallback) --
    if (-not $Url) {
        Write-Host "ERROR: no manager URL. Set MANAGER_URL in .env or pass -Url <url>." -ForegroundColor Red
        exit 1
    }

    # -- Ensure venv --
    # `uv sync` creates .venv itself and installs exactly what uv.lock pins,
    # so it covers both the missing-venv and the stale-venv case. Installing
    # single packages by hand here would bypass the lock.
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Host "Creating .venv from pyproject.toml + uv.lock..." -ForegroundColor Cyan
        uv sync --python 3.12
    }
    else {
        & .\.venv\Scripts\python.exe -c "import textual, httpx" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Syncing dependencies..." -ForegroundColor Cyan
            uv sync
        }
    }

    $runArgs = @("manager_tui.py", "--url", $Url, "--token", $Token, "--t2g-token", $T2gToken)
    & .\.venv\Scripts\python.exe @runArgs
} finally {
    Pop-Location
}
