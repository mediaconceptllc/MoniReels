# Builds the full Windows release: PyInstaller backend one-folder build,
# Flutter release build, then copies the backend into
# frontend/build/windows/x64/runner/Release/backend/ so the packaged
# frontend exe can find and spawn it (see lib/application/backend_launcher.dart).
#
# Run from anywhere; paths are resolved relative to this script's location.
$root = $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"

Write-Host "== Building backend (PyInstaller) ==" -ForegroundColor Cyan
Push-Location $backendDir
try {
    if (-not (Test-Path ".venv")) {
        throw "backend/.venv not found. Run: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    }
    & ".\.venv\Scripts\python.exe" -m PyInstaller backend.spec --distpath dist --workpath build_pyinstaller --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
} finally {
    Pop-Location
}

Write-Host "== Building frontend (flutter build windows --release) ==" -ForegroundColor Cyan
Push-Location $frontendDir
try {
    flutter build windows --release
    if ($LASTEXITCODE -ne 0) { throw "Flutter build failed" }
} finally {
    Pop-Location
}

Write-Host "== Bundling backend into the release output ==" -ForegroundColor Cyan
$releaseDir = Join-Path $frontendDir "build\windows\x64\runner\Release"
$backendDist = Join-Path $backendDir "dist\ai_video_editor_backend"
$bundledBackendDir = Join-Path $releaseDir "backend"

if (-not (Test-Path $releaseDir)) { throw "Release output not found at $releaseDir" }
Remove-Item $bundledBackendDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $bundledBackendDir | Out-Null
Copy-Item "$backendDist\*" -Destination $bundledBackendDir -Recurse -Force

Write-Host "== Done ==" -ForegroundColor Green
Write-Host "Release build: $releaseDir\ai_video_editor_frontend.exe"
Write-Host "The backend is bundled at: $bundledBackendDir\ai_video_editor_backend.exe"
Write-Host "Note: ffmpeg.exe / ffprobe.exe are NOT bundled -- they must be on PATH,"
Write-Host "placed in a bin\ folder next to the backend exe, or pointed to via FFMPEG_PATH."
