# Building the Windows installer

This turns the app into a single **`VidAudDownloader-Setup.exe`** your friends
double-click → Next, Next, Finish → get a desktop / Start-menu icon. No Python,
no pip, no console on their side.

## One-time setup (on your build PC)

1. Python 3.12 on PATH with the app's deps:
   ```powershell
   pip install -U "yt-dlp[default]" PySide6 bgutil-ytdlp-pot-provider
   ```
2. Inno Setup 6 (builds the installer):
   ```powershell
   winget install JRSoftware.InnoSetup
   ```
3. ffmpeg present at `C:\Program Files (x86)\ffmpeg\bin`
   (otherwise edit `$FfmpegBin` near the top of `build.ps1`).

PyInstaller and Pillow are installed automatically by the build script.

## Build

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

The result is **`dist\VidAudDownloader-Setup.exe`** — that's the only file you
send people.

## What ends up installed on a friend's PC

Per-user, in `%LOCALAPPDATA%\Programs\VidAudDownloader\` (no admin needed):

- `VidAudDownloader.exe` + `_internal\` — the frozen GUI (Qt etc.)
- `lib\` — **yt-dlp shipped loose so it can auto-update** (not frozen in)
- `runtime\` — the portable Node.js + PO-token server
- `ffmpeg\` — bundled `ffmpeg.exe` / `ffprobe.exe`

## How it keeps working (yt-dlp auto-update)

YouTube breaks stale yt-dlp within weeks. A few seconds after launch the app
quietly checks PyPI, and if there's a newer yt-dlp it downloads it into
`lib_update\`. The update is swapped into `lib\` the **next** time the app
opens. Your friends never touch pip or a console — and you don't have to
rebuild and re-send the installer every time YouTube changes.

(The footer **Update yt-dlp** button does the same check on demand.)

## Uninstalling (for your friends)

Three equally easy ways — pick whichever they find:

- **Settings → Apps → Installed apps →** *Video & Audio Downloader* → Uninstall
- **Start menu →** *Video & Audio Downloader* folder → **Uninstall**
- Control Panel → Programs and Features → Uninstall

It removes everything, including downloaded yt-dlp updates and saved settings.
Their downloaded videos (in whatever folder they chose) are left untouched.

## The one rough edge: SmartScreen

Because the installer isn't code-signed, the first person to run it sees a blue
**"Windows protected your PC"** box. It's not a virus warning — it's just
"unknown publisher." Tell friends:

> Click **More info**, then **Run anyway**.

The warning fades as more people download it. To remove it entirely you'd need a
code-signing certificate (e.g. Azure Trusted Signing, ~$10/month).

## Releasing a new version

Bump `#define AppVersion` in `installer.iss`, rebuild, send the new
`...-Setup.exe`. Installing over an existing copy upgrades it in place (same
`AppId`).
