<#
.SYNOPSIS
    Build pipeline for Audio Auto-Leveler.

.DESCRIPTION
    Bumps version, builds the exe via PyInstaller, and archives the output
    into a versioned zip under the builds/ folder.

.PARAMETER BumpPart
    Which part of the version to bump: major, minor, or patch (default: patch).

.PARAMETER NoBump
    Skip version bump (rebuild current version).

.EXAMPLE
    .\build.ps1                   # bump patch  → 1.0.1, build, zip
    .\build.ps1 -BumpPart minor   # bump minor  → 1.1.0, build, zip
    .\build.ps1 -NoBump           # rebuild current version
#>
param(
    [ValidateSet("major", "minor", "patch")]
    [string]$BumpPart = "patch",
    [switch]$NoBump
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$SourceFile  = Join-Path $ProjectDir "audio_monitor.py"
$SpecFile    = Join-Path $ProjectDir "audio_leveler.spec"
$BuildsDir   = Join-Path $ProjectDir "builds"

# ── Helpers ──────────────────────────────────────────────────────
function Get-AppVersion {
    $content = Get-Content $SourceFile -Raw
    if ($content -match '__version__\s*=\s*"(\d+\.\d+\.\d+)"') {
        return $Matches[1]
    }
    throw "Could not find __version__ in $SourceFile"
}

function Set-AppVersion([string]$NewVersion) {
    $content = Get-Content $SourceFile -Raw
    $content = $content -replace '__version__\s*=\s*"[\d.]+"', "__version__ = `"$NewVersion`""
    Set-Content -Path $SourceFile -Value $content -NoNewline -Encoding UTF8
}

function Bump-Version([string]$Current, [string]$Part) {
    $parts = $Current.Split(".")
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    $patch = [int]$parts[2]

    switch ($Part) {
        "major" { $major++; $minor = 0; $patch = 0 }
        "minor" { $minor++; $patch = 0 }
        "patch" { $patch++ }
    }
    return "$major.$minor.$patch"
}

# ── 1. Version bump ─────────────────────────────────────────────
$currentVersion = Get-AppVersion
Write-Host ""
Write-Host "=== Audio Auto-Leveler Build Pipeline ===" -ForegroundColor Cyan
Write-Host "    Current version: $currentVersion"

if (-not $NoBump) {
    $newVersion = Bump-Version $currentVersion $BumpPart
    Set-AppVersion $newVersion
    Write-Host "    Bumped to:       $newVersion ($BumpPart)" -ForegroundColor Green
    $version = $newVersion
} else {
    Write-Host "    Rebuilding:      $currentVersion (no bump)" -ForegroundColor Yellow
    $version = $currentVersion
}

# ── 2. Pre-flight checks ────────────────────────────────────────
Write-Host ""
Write-Host "[1/4] Checking dependencies..." -ForegroundColor Cyan

$pyinstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyinstaller) {
    Write-Host "  PyInstaller not found on PATH. Installing..." -ForegroundColor Yellow
    python -m pip install pyinstaller --quiet
}

# Resolve pyinstaller — prefer 'python -m PyInstaller' which always works
$PyInstallerCmd = "python -m PyInstaller"

# Verify VLC
$vlcFound = $false
foreach ($pf in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($pf -and (Test-Path (Join-Path $pf "VideoLAN\VLC\libvlc.dll"))) {
        $vlcFound = $true
        Write-Host "  VLC found: $(Join-Path $pf 'VideoLAN\VLC')" -ForegroundColor Green
        break
    }
}
if (-not $vlcFound) {
    throw "VLC not found. Install 64-bit VLC first."
}

Write-Host "  All checks passed." -ForegroundColor Green

# ── 3. Build ────────────────────────────────────────────────────
Write-Host ""
Write-Host "[2/4] Building with PyInstaller..." -ForegroundColor Cyan

Push-Location $ProjectDir
try {
    # Clean previous build
    if (Test-Path (Join-Path $ProjectDir "build")) {
        Remove-Item (Join-Path $ProjectDir "build") -Recurse -Force
    }
    if (Test-Path (Join-Path $ProjectDir "dist")) {
        Remove-Item (Join-Path $ProjectDir "dist") -Recurse -Force
    }

    $env:PYTHONIOENCODING = "utf-8"
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $buildOutput = & python -m PyInstaller "$SpecFile" --noconfirm 2>&1
    $buildExitCode = $LASTEXITCODE
    $ErrorActionPreference = $prevPref
    
    foreach ($line in $buildOutput) {
        $text = "$line"
        if ($text -match "ERROR") {
            Write-Host "  $text" -ForegroundColor Red
        } elseif ($text -match "WARNING|warn") {
            # suppress noisy warnings
        } else {
            Write-Host "  $text" -ForegroundColor DarkGray
        }
    }
    
    if ($buildExitCode -ne 0) {
        throw "PyInstaller exited with code $buildExitCode"
    }

    $distDir = Join-Path $ProjectDir "dist\AudioLeveler"
    if (-not (Test-Path (Join-Path $distDir "AudioLeveler.exe"))) {
        throw "Build failed - AudioLeveler.exe not found in dist/"
    }

    Write-Host "  Build successful!" -ForegroundColor Green
} finally {
    Pop-Location
}

# ── 4. Package into versioned zip ───────────────────────────────
Write-Host ""
Write-Host "[3/4] Packaging..." -ForegroundColor Cyan

if (-not (Test-Path $BuildsDir)) {
    New-Item -ItemType Directory -Path $BuildsDir | Out-Null
}

$timestamp = Get-Date -Format "yyyyMMdd"
$zipName   = "AudioLeveler_v${version}_${timestamp}.zip"
$zipPath   = Join-Path $BuildsDir $zipName

if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

# Brief delay to release any file locks from the build
Start-Sleep -Seconds 2

Compress-Archive -Path "$distDir\*" -DestinationPath $zipPath -CompressionLevel Optimal
$zipSize = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host "  Created: builds\$zipName ($zipSize MB)" -ForegroundColor Green

# ── 5. Create installer (Inno Setup) ────────────────────────────
Write-Host ""
Write-Host "[4/5] Creating installer..." -ForegroundColor Cyan

$issFile = Join-Path $ProjectDir "installer.iss"
$installerName = "AudioLeveler_v${version}_Setup.exe"

# Find Inno Setup compiler
$isccPaths = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$iscc = $null
foreach ($p in $isccPaths) {
    if (Test-Path $p) { $iscc = $p; break }
}

if ($iscc -and (Test-Path $issFile)) {
    $prevPref2 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $issOutput = & "$iscc" "/DAppVersion=$version" "$issFile" 2>&1
    $issExit = $LASTEXITCODE
    $ErrorActionPreference = $prevPref2
    
    foreach ($line in $issOutput) {
        $text = "$line"
        if ($text -match "Error") {
            Write-Host "  $text" -ForegroundColor Red
        } elseif ($text -match "Successful") {
            Write-Host "  $text" -ForegroundColor Green
        }
    }

    $installerPath = Join-Path $BuildsDir $installerName
    if ($issExit -eq 0 -and (Test-Path $installerPath)) {
        $instSize = [math]::Round((Get-Item $installerPath).Length / 1MB, 1)
        Write-Host "  Created: builds\$installerName ($instSize MB)" -ForegroundColor Green
    } else {
        Write-Host "  Installer build failed (exit $issExit). Zip is still available." -ForegroundColor Yellow
    }
} else {
    Write-Host "  Inno Setup not found. Skipping installer. Zip-only build." -ForegroundColor Yellow
    Write-Host "  Install via: winget install JRSoftware.InnoSetup" -ForegroundColor DarkGray
}

# ── 6. Summary ──────────────────────────────────────────────────
Write-Host ""
Write-Host "[5/5] Done!" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  App:     Audio Auto-Leveler" -ForegroundColor White
Write-Host "  Version: $version" -ForegroundColor White
Write-Host "  Zip:     builds\$zipName ($zipSize MB)" -ForegroundColor White
if ($iscc -and (Test-Path (Join-Path $BuildsDir $installerName))) {
    Write-Host "  Setup:   builds\$installerName ($instSize MB)" -ForegroundColor White
}
Write-Host "  Exe:     dist\AudioLeveler\AudioLeveler.exe" -ForegroundColor White
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""
