# ============================================
# Token Dashboard - Windows Installer
# ============================================
# Usage:
#   powershell -Command "irm https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/install-hook.ps1 | iex"
# ============================================

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Token Dashboard Installer (Windows)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# --- 1. Check prerequisites ---
Write-Host "[1/5] Checking prerequisites..." -ForegroundColor Yellow

# Node.js check
$nodePath = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodePath) {
    Write-Host "  [ERROR] Node.js not found." -ForegroundColor Red
    Write-Host "  Install: https://nodejs.org/ (LTS recommended)" -ForegroundColor Red
    Write-Host "  Or run: winget install OpenJS.NodeJS.LTS" -ForegroundColor Gray
    exit 1
}
$nodeVersion = node -v
Write-Host "  Node.js $nodeVersion" -ForegroundColor Green

# npm check
$npmPath = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npmPath) {
    Write-Host "  [ERROR] npm not found." -ForegroundColor Red
    exit 1
}
$npmVersion = npm -v
Write-Host "  npm v$npmVersion" -ForegroundColor Green

# Git check
$gitPath = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitPath) {
    Write-Host "  [ERROR] Git not found." -ForegroundColor Red
    Write-Host "  Install: https://git-scm.com/download/win" -ForegroundColor Red
    Write-Host "  Or run: winget install Git.Git" -ForegroundColor Gray
    exit 1
}
Write-Host "  Git OK" -ForegroundColor Green

# --- 2. Clone ---
Write-Host ""
Write-Host "[2/5] Cloning repository..." -ForegroundColor Yellow

$installDir = Join-Path $HOME "token-dashboard"

if (Test-Path $installDir) {
    Write-Host "  Directory already exists: $installDir" -ForegroundColor Gray
    Write-Host "  Pulling latest changes..." -ForegroundColor Gray
    Push-Location $installDir
    git pull origin main 2>$null
    Pop-Location
} else {
    # Sparse checkout - token-dashboard folder only
    git clone https://github.com/EO-Studio-Dev/token-dashboard-hooks.git $installDir 2>$null
    Push-Location $installDir
    git sparse-checkout set token-dashboard
    Pop-Location

    # Move contents up one level
    $tempDir = Join-Path $HOME "token-dashboard-temp"
    if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force }
    Rename-Item $installDir $tempDir
    $srcDir = Join-Path $tempDir "token-dashboard"
    if (Test-Path $srcDir) {
        Copy-Item $srcDir $installDir -Recurse
        Remove-Item $tempDir -Recurse -Force
    } else {
        Rename-Item $tempDir $installDir
    }
}
Write-Host "  Installed to: $installDir" -ForegroundColor Green

# --- 3. Install dependencies ---
Write-Host ""
Write-Host "[3/5] Installing dependencies (npm install)..." -ForegroundColor Yellow
Push-Location $installDir
npm install 2>$null
Pop-Location
Write-Host "  Dependencies installed" -ForegroundColor Green

# --- 4. Setup .env.local ---
Write-Host ""
Write-Host "[4/5] Setting up environment..." -ForegroundColor Yellow

$envFile = Join-Path $installDir ".env.local"
$envExample = Join-Path $installDir ".env.example"

if (Test-Path $envFile) {
    Write-Host "  .env.local already exists, skipping" -ForegroundColor Gray
} elseif (Test-Path $envExample) {
    Copy-Item $envExample $envFile
    Write-Host "  Created .env.local from .env.example" -ForegroundColor Green
    Write-Host "  [ACTION REQUIRED] Edit .env.local with your API key:" -ForegroundColor Red
    Write-Host "    notepad $envFile" -ForegroundColor Gray
} else {
    @"
# Mock data mode (no API key needed)
DATA_SOURCE=mock
"@ | Out-File -FilePath $envFile -Encoding utf8
    Write-Host "  Created .env.local with mock data mode" -ForegroundColor Green
}

# --- 5. Done ---
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Installation Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Location: $installDir" -ForegroundColor White
Write-Host ""
Write-Host "  To start:" -ForegroundColor White
Write-Host "    cd $installDir" -ForegroundColor Yellow
Write-Host "    npm run dev" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Then open: http://localhost:3000" -ForegroundColor Cyan
Write-Host ""
