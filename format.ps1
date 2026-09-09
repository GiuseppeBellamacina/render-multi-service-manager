# Format and lint all code with Isort, Black and Ruff.
#
# Tools come from .venv (created by `uv sync`), never from PATH: a globally
# installed black/ruff of a different version would reformat the whole repo.
#
# PYTHONUTF8=1 is REQUIRED on Windows: the TUI contains box-drawing and status
# glyphs, and under the default cp1252 console encoding isort fails to read the
# file and SKIPS it with only a UserWarning ("charmap codec can't encode"),
# silently leaving imports unsorted while still exiting 0.

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

Push-Location $PSScriptRoot
try {
    $venv = Join-Path $PSScriptRoot ".venv\Scripts"
    if (-not (Test-Path (Join-Path $venv "ruff.exe"))) {
        Write-Host "No .venv found - run: uv sync" -ForegroundColor Red
        exit 1
    }

    Write-Host "================================"
    Write-Host "  Code Formatting & Linting"
    Write-Host "================================"
    Write-Host ""

    Write-Host "Running Isort..." -ForegroundColor Cyan
    & (Join-Path $venv "isort.exe") .
    Write-Host "Isort completed" -ForegroundColor Green

    Write-Host ""

    Write-Host "Running Black formatter..." -ForegroundColor Cyan
    & (Join-Path $venv "black.exe") .
    Write-Host "Black formatting completed" -ForegroundColor Green

    Write-Host ""

    Write-Host "Running Ruff linter with auto-fix..." -ForegroundColor Cyan
    & (Join-Path $venv "ruff.exe") check --fix .
    $ruffExit = $LASTEXITCODE
    if ($ruffExit -eq 0) {
        Write-Host "Ruff linting completed" -ForegroundColor Green
    }
    else {
        Write-Host "Ruff found issues that need manual fixing" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "================================"
    Write-Host "  Formatting Complete!"
    Write-Host "================================"

    exit $ruffExit
}
finally {
    Pop-Location
}
