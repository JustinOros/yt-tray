# yt-tray

A system tray app that plays YouTube playlists in the background, audio only and ad-free.

yt-dlp resolves the raw audio stream and mpv plays it, so no ad is ever requested. Runs on Windows, macOS and Linux as a single Python script.

## Features

- Tray icon toggles Play/Pause on left-click (middle-click on GNOME), and the icon shows the state (grey idle, blue loading, green playing, amber paused)
- First Play with nothing queued opens a small search box
- Right-click menu: Play/Pause, Search, Next, Previous, Now Playing (current title), Shuffle toggle, Quit
- Search finds a YouTube playlist from your text and starts playing it, shuffled or in order
- Recovers on its own from stalls, errors and expired stream URLs by re-resolving and resuming at the same position
- Remembers the last playlist, track and position between launches
- Updates yt-dlp automatically (at most once a day, and immediately after repeated failures)

## Spotify playlists

Open the Spotify menu, choose Load Playlist, and paste a public playlist link, for example https://open.spotify.com/playlist/6ZYVuIwQDiHbKIWf0LHTbz. Loaded playlists appear in the menu and can be removed from the Remove submenu. Nothing plays from Spotify: tracks are read from the playlist's public page and each one is matched to a YouTube video by artist, title and length, then played and prefetched like any other playlist. Spotify only exposes the first 100 tracks of a playlist this way, and private playlists cannot be loaded.

## Quick start

```
python3 yt-tray.py
```

On first run the script creates a `.venv` next to itself, installs the Python packages there, installs mpv and the other system pieces with your package manager (Homebrew, apt, dnf, pacman, zypper), and relaunches itself inside the venv. On Windows it also tries to download libmpv automatically. Linux package installs use sudo and need a terminal. Run `python3 yt-tray.py --setup` to do only the setup and exit.

## Manual install

You need Python 3.9 or newer, mpv's library (libmpv), and tkinter for the search box.

### Windows

1. Install Python from python.org (tkinter is included).
2. Get libmpv: from the Windows builds linked on https://mpv.io/installation download the `mpv-dev` archive for x86_64 and extract `libmpv-2.dll` (or `mpv-2.dll`) into the same folder as `yt-tray.py`.
3. In a terminal inside the repo folder:

```
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw.exe yt-tray.py
```

### macOS

```
brew install mpv python-tk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python yt-tray.py
```

On macOS the menu bar icon opens the menu on click, so Play/Pause is the first menu item. The system tray library cannot bind a separate left-click action there.

### Linux

Debian and Ubuntu:

```
sudo apt install mpv libmpv2 python3-venv python3-tk python3-gi gir1.2-ayatanaappindicator3-0.1
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python yt-tray.py
```

Fedora:

```
sudo dnf install mpv-libs python3-tkinter python3-gobject libayatana-appindicator-gtk3
```

Arch:

```
sudo pacman -S mpv tk python-gobject libayatana-appindicator
```

On older Ubuntu releases the package is `libmpv1` instead of `libmpv2`. GNOME needs the AppIndicator extension to show tray icons. With the AppIndicator backend (GNOME) a left-click always opens the menu, so middle-click the icon to toggle Play/Pause, or use the first menu item. Desktops using the plain X11 tray (KDE, XFCE, MATE and others) get the left-click toggle.

## Check that it works

```
python yt-tray.py --selftest "lofi hip hop"
```

This searches for a playlist, resolves the first track, plays it silently for a few seconds and prints PASS or FAIL. Use the Python from your venv.

## Start at login

The easy way, on any OS:

```
python3 yt-tray.py --install-service
python3 yt-tray.py --remove-service
```

Install registers a login item (Windows Run key, macOS LaunchAgent, Linux autostart entry). Remove undoes it. Do not move the folder afterward. The manual steps below do the same thing.

Run these from inside the repo folder after installing.

### Windows (Command Prompt)

```
reg add HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v yt-tray /t REG_SZ /d "\"%CD%\.venv\Scripts\pythonw.exe\" \"%CD%\yt-tray.py\"" /f
```

Remove it with `reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v yt-tray /f`.

### macOS

```
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.justinoros.yt-tray.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.justinoros.yt-tray</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PWD/.venv/bin/python</string>
    <string>$PWD/yt-tray.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.justinoros.yt-tray.plist
```

Remove it with `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.justinoros.yt-tray.plist`.

### Linux

```
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/yt-tray.desktop <<EOF
[Desktop Entry]
Type=Application
Name=yt-tray
Exec=$PWD/.venv/bin/python $PWD/yt-tray.py
X-GNOME-Autostart-enabled=true
EOF
```

Avoid spaces in the repo path for the macOS and Linux snippets.

## Files

State, log and settings live here:

- Windows: `%APPDATA%\yt-tray`
- macOS: `~/Library/Application Support/yt-tray`
- Linux: `~/.config/yt-tray`

`state.json` holds the playlist, order, track and position. `yt-tray.log` records errors and recoveries. Set `YT_TRAY_HOME` to use another folder.

## Stop and restart

```
python3 yt-tray.py --stop
python3 yt-tray.py --restart
```

`--stop` quits the running copy. `--restart` quits it if running, then starts a fresh one.

## Notes

- yt-dlp needs a JavaScript runtime for YouTube. `requirements.txt` installs Deno through pip, and the app puts the venv on the PATH for yt-dlp, so nothing else is required.
- If YouTube asks you to sign in or prove you are not a bot, pass cookies to yt-dlp with the `YT_TRAY_YTDLP_ARGS` environment variable, for example `--cookies-from-browser firefox`.
- Update yt-dlp by hand at any time with `python yt-tray.py --update`.
- Only one copy runs at a time.
