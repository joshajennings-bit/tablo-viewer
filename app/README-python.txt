TABLO VIEWER for Windows  (unofficial)
======================================

Watch live TV from your Tablo 4th Gen on a Windows 10/11 PC.
Not made or supported by Tablo. It uses the same undocumented API as the
official apps, so a Tablo update could break it.


SETUP (one time, about 2 minutes)
---------------------------------
1. Unzip this folder somewhere permanent, e.g.  Documents\TabloViewer
   (don't run it from inside the zip).
2. Double-click  Setup.bat
   - Uses Python 3.9+ if you have it, or offers to install it with winget.
   - Installs two small packages (requests, keyring) into a private folder
     inside TabloViewer (.venv). Nothing else on your PC is changed.
   - Offers to install the mpv video player if you don't have it.
3. Double-click  Start Tablo Viewer.bat
   - Sign in with the same email and password you use in the Tablo app.
   - Your PC must be on the same home network as the Tablo (not a VPN).

Tip: right-click "Start Tablo Viewer.bat" > Send to > Desktop (create shortcut).


USING IT
--------
- Click a channel in the guide on the right to start watching. Video plays
  inside the window; the bar underneath shows what's on now and next.
- Fullscreen (video only): double-click the video, press F or F11, or click
  the fullscreen button. Esc or double-click to come back. In fullscreen,
  move the mouse over the video for mpv's on-screen controls.
- Volume: slider at bottom right, mouse wheel over the video, or + / -.
- "Free streaming channels" adds Tablo's free internet channels.
- Each channel you watch uses one Tablo tuner.

Keyboard (click the guide or controls first so the app has focus):
    Space = pause/play        Left / Right = back 10 s / forward 30 s
    Up / Down = channel       M = mute         L = jump back to live
    F or F11 = fullscreen     Esc = leave fullscreen
    / = search channels       F5 = refresh
The same keys work when the video itself has focus.

IF SOMETHING GOES WRONG
-----------------------
- "Couldn't reach your Tablo": PC and Tablo must be on the same network.
  Turn off VPNs and check Windows Firewall isn't blocking Python.
- "Tuners are busy": something else is recording or watching.
- No picture: make sure mpv is installed (run Setup.bat again), or use
  the ... menu > Locate mpv.exe. VLC is no longer used.
- The ... menu > Open log folder shows tablo_viewer.log and mpv.log
  (passwords aren't logged).
- Settings live in %APPDATA%\TabloViewer. Your password, if you tick
  "Remember me", is stored in Windows Credential Manager under "TabloViewer".


SECURITY SOFTWARE (SentinelOne etc.)
------------------------------------
This is plain, readable Python source run by the standard Python
interpreter. There is no packed .exe, no installer, no admin rights, and
no background service. It only talks to:
  - lighthousetv.ewscloud.com  (Tablo's sign-in and guide service)
  - your Tablo box on your home network
If your PC is managed by an employer, their policy may still block it;
ask IT rather than trying to work around it.


FILES
-----
tablo_viewer.py         the app window
tablo_client.py         talks to Tablo's service and your Tablo box
mpv_player.py           runs and controls the built-in video player
Setup.bat               one-time setup
Start Tablo Viewer.bat  starts the app
requirements.txt        Python packages used

Credits: API details from the open-source tablo-api (Trevor Viljoen, MIT)
and tablo2plex (hearhellacopters) projects.
