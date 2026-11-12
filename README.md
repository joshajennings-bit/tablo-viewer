# Tablo Viewer

Unofficial Windows app for watching live TV from a **Tablo 4th Gen** on your home network.
Channel guide, now/next info, pause and rewind, volume, and video-only fullscreen, all in one window.

> Not made or supported by Tablo, Nuvyyo or Scripps. It uses the same undocumented API as
> the official apps, so a Tablo update could break it.

## Download

Go to **[Releases → latest](../../releases/tag/latest)** and download one of:

| File | What it is |
|---|---|
| `TabloViewer.exe` | Single file. Double-click to run. Nothing to install. |
| `TabloViewer-portable.zip` | Same app as a folder. Starts faster. Unzip, then run `TabloViewer.exe` inside. |

Sign in with your Tablo app email and password. Your PC must be on the same network as the Tablo.

Windows may show a SmartScreen warning the first time ("Windows protected your PC") because the
app isn't code-signed. Click **More info → Run anyway**.

## Keyboard

| Key | Action |
|---|---|
| Space | Pause / play |
| ← / → | Back 10 s / forward 30 s |
| ↑ / ↓ | Channel up / down |
| + / − or mouse wheel | Volume |
| M | Mute |
| C | Closed captions on / off (off by default) |
| L | Jump back to live |
| F, F11 or double-click | Fullscreen (video only) |
| Esc | Leave fullscreen |
| / | Search channels |

Settings and logs: `%APPDATA%\TabloViewer`. If you tick "Remember me", the password is kept in
Windows Credential Manager.

## How it's built

Every push to `main` runs [`.github/workflows/build.yml`](.github/workflows/build.yml) on a
GitHub-hosted Windows machine: it installs Python, downloads the latest
[mpv](https://mpv.io) Windows build from
[shinchiro/mpv-winbuild-cmake](https://gh.hhj520.top/shinchiro/mpv-winbuild-cmake), packages
everything with [PyInstaller](https://pyinstaller.org), runs a self-test, and publishes the result
to the `latest` release. You can also start a build from the **Actions** tab ("Run workflow").

Source is in [`app/`](app). To run it from source instead, see [`app/README-python.txt`](app/README-python.txt).

## Credits

API details from the open-source [tablo-api](https://gh.hhj520.top/trevor-viljoen/tablo-api)
(MIT) and [tablo2plex](https://gh.hhj520.top/hearhellacopters/tablo2plex) projects.
Video playback by [mpv](https://mpv.io) (GPLv2+).
