# build.ps1 — one command to produce the shareable installer.
#
#   Run from this folder:
#       powershell -ExecutionPolicy Bypass -File .\build.ps1
#
# Output: dist\VidAudDownloader-Setup.exe  (give THIS file to your friends).
#
# Requirements (one-time):
#   * Python 3.12 on PATH with the app's deps installed
#       pip install -U "yt-dlp[default]" PySide6 bgutil-ytdlp-pot-provider
#   * Inno Setup 6   ->  winget install JRSoftware.InnoSetup
#   * ffmpeg present at $FfmpegBin below (edit if yours lives elsewhere)
# PyInstaller and Pillow are installed automatically by this script.

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

$AppShort  = 'VidAudDownloader'
$Py        = (Get-Command python).Source
$Dist      = Join-Path $Root 'dist'
$Build     = Join-Path $Root 'build'
$AppDir    = Join-Path $Dist $AppShort          # PyInstaller onedir output
$FfmpegBin = 'C:\Program Files (x86)\ffmpeg\bin'

function Step($n, $msg) { Write-Host "`n== $n  $msg ==" -ForegroundColor Cyan }
function Die($msg)       { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

function RemoveTree($p) {
    # Robust recursive delete: clear read-only first (git pack files shipped
    # inside runtime\ are read-only and otherwise make Remove-Item fail).
    if (-not (Test-Path $p)) { return }
    Get-ChildItem -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReadOnly } |
        ForEach-Object { $_.Attributes = $_.Attributes -bxor [IO.FileAttributes]::ReadOnly }
    Remove-Item -LiteralPath $p -Recurse -Force
}

# --- 1. build tools ---------------------------------------------------------
Step '1/7' 'Ensuring PyInstaller + Pillow'
& $Py -m pip install --upgrade --quiet pyinstaller pillow
if ($LASTEXITCODE -ne 0) { Die 'failed to install build tools' }

# --- 2. clean ---------------------------------------------------------------
Step '2/7' 'Cleaning previous output'
RemoveTree $AppDir
RemoveTree (Join-Path $Build $AppShort)
New-Item -ItemType Directory -Force -Path $Build | Out-Null

# --- 3. icon ----------------------------------------------------------------
Step '3/7' 'Generating icon.ico from the app logo'
$env:QT_QPA_PLATFORM = 'offscreen'
& $Py (Join-Path $Root 'make_icon.py')
if ($LASTEXITCODE -ne 0) { Die 'icon generation failed' }

# --- 4. freeze the app (yt-dlp excluded — see the .spec) --------------------
Step '4/7' 'Running PyInstaller'
& $Py -m PyInstaller --noconfirm --clean (Join-Path $Root 'VidAudDownloader.spec')
if ($LASTEXITCODE -ne 0) { Die 'PyInstaller failed' }
$Exe = Join-Path $AppDir "$AppShort.exe"
if (-not (Test-Path $Exe)) { Die "PyInstaller did not produce $Exe" }

# --- 5. drop the updatable yt-dlp + runtime + ffmpeg next to the .exe -------
Step '5/7' 'Bundling lib (yt-dlp), runtime, ffmpeg'
$Lib = Join-Path $AppDir 'lib'
if (Test-Path $Lib) { Remove-Item $Lib -Recurse -Force }
& $Py -m pip install --upgrade --target $Lib "yt-dlp[default]" bgutil-ytdlp-pot-provider
if ($LASTEXITCODE -ne 0) { Die 'failed to populate lib\ with yt-dlp' }
# console-script stubs aren't needed inside the app
$libBin = Join-Path $Lib 'bin'
if (Test-Path $libBin) { Remove-Item $libBin -Recurse -Force }

$srcRuntime = Join-Path $Root 'runtime'
if (-not (Test-Path $srcRuntime)) { Die "runtime\ folder not found next to this script" }
# robocopy (not Copy-Item) so we can skip the bgutil checkout's .git folder —
# it's useless to ship, bloats the installer, and its read-only pack files break
# the next clean. /E = all subdirs incl. empty; codes < 8 are success.
robocopy $srcRuntime (Join-Path $AppDir 'runtime') /E /XD .git /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Die 'failed to copy runtime\' }
$global:LASTEXITCODE = 0

$FfDir = Join-Path $AppDir 'ffmpeg'
New-Item -ItemType Directory -Force -Path $FfDir | Out-Null
foreach ($f in @('ffmpeg.exe', 'ffprobe.exe')) {
    $src = Join-Path $FfmpegBin $f
    if (-not (Test-Path $src)) { Die "missing $src  (edit `$FfmpegBin in build.ps1)" }
    Copy-Item $src $FfDir -Force
}

# --- 6. locate Inno Setup ---------------------------------------------------
Step '6/7' 'Locating Inno Setup (ISCC.exe)'
$Iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
    foreach ($p in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'))) {
        if (Test-Path $p) { $Iscc = $p; break }
    }
}
if (-not $Iscc) { Die 'Inno Setup not found. Install it:  winget install JRSoftware.InnoSetup' }

# --- 7. build the installer -------------------------------------------------
Step '7/7' 'Compiling the installer'
& $Iscc (Join-Path $Root 'installer.iss')
if ($LASTEXITCODE -ne 0) { Die 'Inno Setup compile failed' }

$Setup = Join-Path $Dist "$AppShort-Setup.exe"
Write-Host "`nDONE." -ForegroundColor Green
Write-Host "Share this file:  $Setup" -ForegroundColor Green
