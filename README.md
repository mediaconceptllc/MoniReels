# autoReel

autoReel turns a long video into short-form clips automatically. Import a
video, it transcribes the audio, an AI picks the best moments and writes
hooks/captions for you, and you export ready-to-post Shorts/Reels plus a
condensed YouTube version — all from one desktop app on Windows.

## Download

**[Download autoReel for Windows (autoReel-Setup-1.0.0.exe)](https://github.com/tuvshinorg/autoReel/releases/latest)**

- Windows 10/11, 64-bit
- No admin rights needed to install
- Nothing else to install separately — the app, its backend, and FFmpeg are
  all bundled in the installer

Run the downloaded `.exe`, click through the installer, and launch autoReel
from the Start Menu (or the desktop shortcut, if you checked that box).

> Windows SmartScreen may warn about an "unrecognized app" the first time,
> since this build isn't code-signed yet. Click **More info → Run anyway**.

## What it does

1. **Import** a video file.
2. **Transcribe** — automatic speech-to-text (Mongolian, via
   [Chimege](https://chimege.mn)).
3. **AI suggestions** — an AI reads the transcript and proposes 3 short-form
   edits (hook, context, proof, payoff) plus long-form YouTube highlight
   reels for videos over 20 minutes. Choose **OpenAI (ChatGPT)** or
   **Claude** as the provider — switchable anytime in Settings, or per
   regeneration with the "Regenerate with ChatGPT / Regenerate with Claude"
   buttons.
4. **Review & edit** — fix any transcription mistakes, or hand-pick your own
   lines to build a custom clip.
5. **Export** — renders each suggestion as its own video file, with
   transitions, captions, and your choice of hardware encoder if available.

## First-time setup

After installing, open **Settings** inside the app and paste in:

- A **Chimege API token** — get one at [console.chimege.com](https://console.chimege.com)
  (required for transcription)
- An **OpenAI API key** ([platform.openai.com](https://platform.openai.com)) and/or
  an **Anthropic (Claude) API key** ([console.anthropic.com](https://console.anthropic.com))
  — you only need one to get started, but having both lets you compare
  results from each provider

Credentials are saved to a local `.env` file next to the installed app and
are never sent anywhere except directly to Chimege/OpenAI/Anthropic's own
APIs.

## Building from source

The Windows installer above is the easiest way to run autoReel. If you'd
rather build it yourself:

**Backend** (Python 3.11, FastAPI)
```
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m app.main
```

**Frontend** (Flutter, Windows desktop)
```
cd frontend
flutter pub get
flutter run -d windows
```

With both running, the desktop app talks to the local backend automatically
(see `frontend/lib/application/backend_launcher.dart`).

To build the same installer this repo publishes, see `installer/setup.iss`
(requires [Inno Setup 6](https://jrsoftware.org/isinfo.php), PyInstaller for
the backend, and `flutter build windows --release` for the frontend).

## Credits

- Speech-to-text by [Chimege](https://chimege.mn)
- Suggestion generation via [OpenAI](https://openai.com) or
  [Anthropic Claude](https://www.anthropic.com)
- Video processing via [FFmpeg](https://ffmpeg.org) (bundled in the
  installer; FFmpeg is free software licensed under the LGPL/GPL — see
  [ffmpeg.org/legal.html](https://ffmpeg.org/legal.html))
