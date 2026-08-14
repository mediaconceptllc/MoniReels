# Building the Windows installer

Produces `build/output/autoReel-Setup-<version>.exe` — a single self-contained
installer bundling the Flutter frontend, the PyInstaller-packaged backend,
and FFmpeg/FFprobe.

## 1. Get ffmpeg + ffprobe

Download a Windows build (e.g. the "essentials" build from
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/)) and copy `ffmpeg.exe` +
`ffprobe.exe` into `backend/bin/` (gitignored — not checked in; FFmpeg's own
license means every build/release step fetches it fresh rather than storing
it in git).

## 2. Build the backend

```powershell
cd backend
.venv\Scripts\python.exe -m PyInstaller backend.spec --noconfirm
```

Note: the `pyinstaller.exe` console-script wrapper has been flaky in this
environment (exits 1 with zero output) — use `python -m PyInstaller`
instead, which works reliably.

Produces `backend/dist/ai_video_editor_backend/`.

## 3. Build the frontend

```powershell
cd frontend
flutter build windows --release
```

Produces `frontend/build/windows/x64/runner/Release/`.

## 4. Assemble the payload

Combine into `installer/build/payload/`, matching the layout
`frontend/lib/application/backend_launcher.dart` expects
(`install_dir/ai_video_editor_frontend.exe` next to
`install_dir/backend/ai_video_editor_backend.exe`):

```powershell
$root = "<repo root>"
$payload = "$root\installer\build\payload"
Remove-Item -Recurse -Force $payload -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $payload | Out-Null

Copy-Item "$root\frontend\build\windows\x64\runner\Release\*" -Destination $payload -Recurse -Force
Copy-Item "$root\backend\dist\ai_video_editor_backend" -Destination "$payload\backend" -Recurse -Force
New-Item -ItemType Directory -Force -Path "$payload\backend\bin" | Out-Null
Copy-Item "$root\backend\bin\ffmpeg.exe","$root\backend\bin\ffprobe.exe" -Destination "$payload\backend\bin" -Force
```

## 5. Compile the installer

Requires [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\setup.iss
```

Produces `installer/build/output/autoReel-Setup-<version>.exe`.

## Testing before shipping

Silent-install to a scratch directory and confirm the app actually launches
and spawns its bundled backend, rather than trusting a clean compile alone:

```powershell
$test = "$env:TEMP\autoReel_install_test"
Start-Process ".\installer\build\output\autoReel-Setup-1.0.0.exe" `
  -ArgumentList "/VERYSILENT", "/DIR=`"$test`"", "/NORESTART", "/SUPPRESSMSGBOXES" -Wait
Start-Process "$test\ai_video_editor_frontend.exe" -WorkingDirectory $test
# check the spawned backend responds:
# Invoke-RestMethod http://127.0.0.1:<port>/health   (port is dynamic - see stderr for "Uvicorn running on ...")
# then uninstall: & "$test\unins000.exe" /VERYSILENT
```

## Publishing

This repo publishes builds as GitHub Releases (large-file-friendly, free,
no extra credentials needed since `gh`/git push access already exists):

```powershell
gh release create v<version> "installer\build\output\autoReel-Setup-<version>.exe" `
  --repo tuvshinorg/autoReel --title "autoReel v<version>" --notes "..."
```

Cloudflare R2 is a reasonable alternative if bandwidth/storage limits ever
become an issue, but needs an R2 API token (Account ID + Access Key ID +
Secret Access Key) that isn't set up yet.
