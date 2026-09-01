# ============================================================================
# run_manager_tui.ps1 - Launch the render-service-manager TUI dashboard.
#
# Usage (from the manager folder):
#     .\run_manager_tui.ps1
#     .\run_manager_tui.ps1 -Url https://your-service.onrender.com
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

    # -- Resolve params (CLI > env > defaults) --
    if (-not $Url) { $Url = $Env:MANAGER_URL }
    if (-not $Url) { $Url = "https://render-multi-service-manager.onrender.com" }
    if (-not $Token) { $Token = $Env:MANAGER_AUTH_TOKEN }
    if (-not $T2gToken) { $T2gToken = $Env:T2G_AUTH_TOKEN }

    # -- Ensure venv --
    if (-not (Test-Path ".venv")) {
        Write-Host "Creating .venv..." -ForegroundColor Cyan
        uv venv --python 3.12 .venv
        uv pip install -r requirements.txt --python .venv\Scripts\python.exe
    }

    # -- Ensure textual installed --
    & .\.venv\Scripts\python.exe -c "import textual" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing textual..." -ForegroundColor Cyan
        uv pip install textual httpx --python .venv\Scripts\python.exe --quiet
    }

    $runArgs = @("manager_tui.py", "--url", $Url, "--token", $Token, "--t2g-token", $T2gToken)
    & .\.venv\Scripts\python.exe @runArgs
} finally {
    Pop-Location
}
