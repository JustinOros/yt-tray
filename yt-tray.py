#!/usr/bin/env python3
import argparse
import json
import locale
import logging
import os
import random
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = "yt-tray"
VERSION = "1.0.0"
UPDATE_INTERVAL = 24 * 3600
SAVE_INTERVAL = 5.0
STALL_SECONDS = 15.0
LOAD_GRACE_SECONDS = 20.0
MAX_RECOVERIES = 4
MAX_SKIPS = 5
SEARCH_FILTER_PLAYLISTS = "EgIQAw%3D%3D"
MAX_PLAYLIST_ITEMS = 500
PREFETCH_OFFSETS = (1, 2, 3, 4, 5, -1)
INSTANCE_PORT = 47653
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

log = logging.getLogger(APP)


def config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(os.environ.get("YT_TRAY_HOME") or Path(base) / APP)
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging():
    handlers = [logging.FileHandler(config_dir() / "yt-tray.log", encoding="utf-8")]
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


DEFAULT_STATE = {
    "query": "",
    "playlist_id": "",
    "playlist_title": "",
    "shuffle": True,
    "theme": "auto",
    "notifications": True,
    "spotify": [],
    "orig": [],
    "order": [],
    "index": 0,
    "position": 0.0,
    "last_update_check": 0.0,
}


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_dir() / "state.json"
        self.lock = threading.Lock()
        self.data = dict(DEFAULT_STATE)
        self.load()

    def load(self):
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update({k: loaded[k] for k in DEFAULT_STATE if k in loaded})
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("could not read state, starting fresh")
        self.data["orig"] = [
            e for e in self.data["orig"] if isinstance(e, dict) and e.get("id")
        ]
        ids = {e["id"] for e in self.data["orig"]}
        self.data["order"] = [i for i in self.data["order"] if i in ids]
        if len(self.data["order"]) != len(ids):
            self.data["order"] = [e["id"] for e in self.data["orig"]]
        if not 0 <= int(self.data["index"] or 0) < max(1, len(self.data["order"])):
            self.data["index"] = 0
            self.data["position"] = 0.0

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(self.data), encoding="utf-8")
                os.replace(tmp, self.path)
            except Exception:
                log.exception("could not save state")


class YtdlpError(Exception):
    pass


def ytdlp_env():
    env = dict(os.environ)
    bindir = str(Path(sys.executable).resolve().parent)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def subprocess_flags():
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def ytdlp_json(args, timeout=120):
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-warnings",
        "--socket-timeout",
        "20",
    ]
    cmd += shlex.split(os.environ.get("YT_TRAY_YTDLP_ARGS", ""))
    cmd += args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=ytdlp_env(),
            **subprocess_flags(),
        )
    except subprocess.TimeoutExpired:
        raise YtdlpError("yt-dlp timed out")
    if proc.returncode != 0:
        lines = [l for l in (proc.stderr or "").strip().splitlines() if l.strip()]
        raise YtdlpError(lines[-1] if lines else "yt-dlp failed")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise YtdlpError("yt-dlp returned unreadable output")


def update_ytdlp():
    log.info("updating yt-dlp")
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--quiet",
        "--disable-pip-version-check",
        "yt-dlp[default,deno]",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            **subprocess_flags(),
        )
        if proc.returncode != 0:
            log.warning("yt-dlp update failed: %s", (proc.stderr or "").strip()[-300:])
            return False
        return True
    except Exception:
        log.exception("yt-dlp update crashed")
        return False


class Resolved:
    def __init__(self, url, title="", expire=0.0, duration=None, user_agent=None):
        self.url = url
        self.title = title
        self.expire = expire
        self.duration = duration
        self.user_agent = user_agent
        self.headers = {}
        self.available_at = 0.0

    def stale(self, margin=90):
        return bool(self.expire) and time.time() > self.expire - margin


def watch_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


CLIENT_CHAIN = [None, "android_vr", "ios", "web_safari", "mweb", "tv"]
PREFERRED_CLIENT = {"value": None}


def parse_info(info):
    url = info.get("url")
    headers = info.get("http_headers") or {}
    if not url:
        formats = info.get("requested_formats") or info.get("formats") or []
        usable = [f for f in formats if f.get("url") and f.get("acodec") != "none"]
        if not usable:
            raise YtdlpError("no playable audio stream")
        usable.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0)
        url = usable[-1]["url"]
        headers = usable[-1].get("http_headers") or headers
    match = re.search(r"[?&/]expire[=/](\d+)", url)
    expire = float(match.group(1)) if match else time.time() + 1800
    headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
    res = Resolved(
        url,
        info.get("title") or "",
        expire,
        info.get("duration"),
        headers.get("User-Agent"),
    )
    res.headers = headers
    pool = [info] + list(info.get("requested_formats") or [])
    res.available_at = max(float(f.get("available_at") or 0) for f in pool)
    return res


def probe_stream(url, headers):
    req = urllib.request.Request(url, headers=dict(headers, Range="bytes=0-1"))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read(1)
            return resp.status in (200, 206)
    except Exception as exc:
        log.warning("stream probe failed: %s", exc)
        return False


def resolve(video_id):
    preferred = PREFERRED_CLIENT["value"]
    chain = [preferred] + [c for c in CLIENT_CHAIN if c != preferred]
    first = None
    last_error = None
    for client in chain:
        args = ["-f", "bestaudio/best", "--no-playlist", "-J"]
        if client:
            args += ["--extractor-args", f"youtube:player_client={client}"]
        args.append(watch_url(video_id))
        try:
            res = parse_info(ytdlp_json(args))
        except YtdlpError as exc:
            last_error = exc
            text = str(exc).lower()
            if any(k in text for k in ("unavailable", "private video", "removed", "terminated")):
                raise
            log.warning("client %s failed: %s", client or "default", exc)
            continue
        if first is None:
            first = res
        wait = res.available_at - time.time()
        if 0 < wait <= 20:
            log.info("waiting %.1fs for stream to become available", wait)
            time.sleep(wait + 0.5)
        if probe_stream(res.url, res.headers):
            PREFERRED_CLIENT["value"] = client
            return res
        log.warning("client %s gave a rejected stream", client or "default")
    if first is not None:
        return first
    raise last_error or YtdlpError("could not resolve stream")


def playlist_id_from(text):
    match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", text or "")
    return match.group(1) if match else None


def clean_entries(raw):
    out = []
    seen = set()
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        vid = e.get("id")
        if not vid or len(vid) != 11 or vid in seen:
            continue
        title = e.get("title") or vid
        if title in ("[Private video]", "[Deleted video]"):
            continue
        seen.add(vid)
        out.append({"id": vid, "title": title})
    return out


def load_playlist(pid):
    data = ytdlp_json(
        [
            "--flat-playlist",
            "--playlist-end",
            str(MAX_PLAYLIST_ITEMS),
            "-J",
            f"https://www.youtube.com/playlist?list={pid}",
        ]
    )
    return data.get("title") or pid, clean_entries(data.get("entries"))


def candidates_from_ytdlp(query):
    url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote_plus(query)
        + "&sp="
        + SEARCH_FILTER_PLAYLISTS
    )
    data = ytdlp_json(["--flat-playlist", "--playlist-end", "15", "-J", url])
    found = []
    for e in data.get("entries") or []:
        pid = (
            playlist_id_from(e.get("url"))
            or playlist_id_from(e.get("webpage_url"))
            or (e.get("id") if e.get("ie_key") == "YoutubeTab" else None)
        )
        if pid and not pid.startswith("RD") and pid not in found:
            found.append(pid)
    return found


def candidates_from_html(query):
    url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote_plus(query)
        + "&sp="
        + SEARCH_FILTER_PLAYLISTS
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "CONSENT=YES+1; SOCS=CAI",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        html = resp.read().decode("utf-8", "replace")
    return parse_html_candidates(html)


def parse_html_candidates(html):
    found = []
    pattern = r'"(?:playlistId|contentId)":"((?:PL|OLAK5uy_|UU|FL)[A-Za-z0-9_-]{10,})"'
    for m in re.finditer(pattern, html):
        if m.group(1) not in found:
            found.append(m.group(1))
    return found


def search_playlist(query):
    last_error = None
    for finder in (candidates_from_ytdlp, candidates_from_html):
        try:
            candidates = finder(query)
        except Exception as exc:
            log.warning("%s failed: %s", finder.__name__, exc)
            last_error = exc
            continue
        for pid in candidates[:5]:
            try:
                title, entries = load_playlist(pid)
            except Exception as exc:
                log.warning("playlist %s failed: %s", pid, exc)
                last_error = exc
                continue
            if entries:
                return {"pid": pid, "title": title, "entries": entries}
    try:
        data = ytdlp_json(
            ["--flat-playlist", "-J", f"ytsearch30:{query}"]
        )
        entries = clean_entries(data.get("entries"))
        if entries:
            return {"pid": "", "title": f"Search: {query}", "entries": entries}
    except Exception as exc:
        last_error = exc
    raise YtdlpError(str(last_error) if last_error else "no results")


def spotify_playlist_id(text):
    match = re.search(r"playlist[/:]([A-Za-z0-9]{22})", text or "")
    return match.group(1) if match else None


def parse_spotify_embed(html):
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise YtdlpError("Spotify page format not recognized")
    data = json.loads(match.group(1))
    containers = []

    def walk(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("trackList"), list):
                containers.append(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    if not containers:
        raise YtdlpError("playlist is private or unavailable")
    box = containers[0]
    name = box.get("name") or box.get("title")
    if not name:
        og = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        name = og.group(1) if og else "Spotify playlist"
    entries = []
    seen = set()
    for track in box["trackList"]:
        uri = track.get("uri") or ""
        tid = uri.split(":")[-1]
        title = (track.get("title") or "").strip()
        artist = (track.get("subtitle") or "").replace("\xa0", " ").strip()
        if not tid or not title or tid in seen:
            continue
        seen.add(tid)
        label = f"{artist} - {title}" if artist else title
        duration = (track.get("duration") or 0) / 1000.0
        entries.append(
            {"id": "sp:" + tid, "title": label, "q": label, "dur": duration}
        )
    if not entries:
        raise YtdlpError("playlist has no tracks")
    return name, entries


def fetch_spotify_playlist(pid):
    req = urllib.request.Request(
        f"https://open.spotify.com/embed/playlist/{pid}",
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        raise YtdlpError(f"could not reach Spotify: {exc}")
    return parse_spotify_embed(html)


def find_youtube_for(query, duration=None):
    data = ytdlp_json(["--flat-playlist", "-J", f"ytsearch5:{query} audio"])
    found = [
        e for e in data.get("entries") or [] if e.get("id") and len(e["id"]) == 11
    ]
    if not found:
        raise YtdlpError("no YouTube match")
    if duration:
        for e in found:
            length = e.get("duration")
            if length and abs(length - duration) <= 12:
                return e["id"]
    return found[0]["id"]


def prepare_libmpv():
    import ctypes.util

    if sys.platform == "win32":
        here = str(Path(__file__).resolve().parent)
        dirs = [here] + [d for d in os.environ.get("YT_TRAY_MPV_DIR", "").split(os.pathsep) if d]
        for d in dirs:
            if os.path.isdir(d):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(d)
                    except OSError:
                        pass
    elif sys.platform == "darwin":
        original = ctypes.util.find_library
        if not original("mpv"):
            for candidate in (
                "/opt/homebrew/lib/libmpv.dylib",
                "/usr/local/lib/libmpv.dylib",
            ):
                if os.path.exists(candidate):
                    ctypes.util.find_library = (
                        lambda name, _c=candidate, _o=original: _c
                        if name == "mpv"
                        else _o(name)
                    )
                    break


def make_player(**extra):
    prepare_libmpv()
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass
    import mpv

    opts = dict(
        vid="no",
        ytdl=False,
        idle="yes",
        input_default_bindings=False,
        input_vo_keyboard=False,
        osc=False,
        audio_display="no",
        network_timeout=20,
        stream_lavf_o="reconnect=1,reconnect_streamed=1,reconnect_delay_max=4",
        user_agent=USER_AGENT,
        loglevel="warn",
        log_handler=lambda level, prefix, text: log.warning("mpv %s %s: %s", level, prefix, text.strip()),
    )
    opts.update(extra)
    return mpv.MPV(**opts)


class Controller:
    def __init__(
        self,
        store,
        player,
        resolver=resolve,
        searcher=search_playlist,
        updater=update_ytdlp,
        on_change=None,
        on_notify=None,
        ask_search=None,
        on_track=None,
        finder=None,
        spotify_loader=None,
    ):
        self.store = store
        self.player = player
        self.resolver = resolver
        self.searcher = searcher
        self.updater = updater
        self.on_change = on_change or (lambda: None)
        self.on_notify = on_notify or (lambda msg: None)
        self.ask_search = ask_search or (lambda: None)
        self.on_track = on_track or (lambda video_id, title: None)
        self.last_announced = None
        self.finder = finder or find_youtube_for
        self.spotify_loader = spotify_loader or fetch_spotify_playlist
        self.lock = threading.RLock()
        self.status = "idle"
        self.gen = 0
        self.cur = None
        self.title = ""
        self.last_pos = 0.0
        self.last_dur = None
        self.progress_at = time.time()
        self.progress_pos = None
        self.started_at = 0.0
        self.seen_active = False
        self.recoveries = 0
        self.skips = 0
        self.stop_flag = threading.Event()
        self.last_save = 0.0
        self.last_forced_update = 0.0
        self.resume_from = 0.0
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.failed = {}
        self.prefetch_event = threading.Event()
        threading.Thread(target=self._prefetch_loop, daemon=True).start()
        self.thread = threading.Thread(target=self._watchdog, daemon=True)
        self.thread.start()
        threading.Thread(target=self._auto_update, daemon=True).start()

    @property
    def data(self):
        return self.store.data

    @property
    def shuffle(self):
        return bool(self.data["shuffle"])

    def has_queue(self):
        return bool(self.data["order"])

    def entry_for(self, video_id):
        for e in self.data["orig"]:
            if e["id"] == video_id:
                return e
        return None

    def current_entry(self):
        order = self.data["order"]
        if not order:
            return None
        return self.entry_for(order[self.data["index"] % len(order)])

    def now_title(self):
        if self.title:
            return self.title
        entry = self.current_entry()
        return entry["title"] if entry else ""

    def _changed(self):
        try:
            self.on_change()
        except Exception:
            log.exception("on_change failed")

    def _auto_update(self):
        last = float(self.data.get("last_update_check") or 0)
        if time.time() - last < UPDATE_INTERVAL:
            return
        ok = self.updater()
        if ok:
            self.data["last_update_check"] = time.time()
            self.store.save()

    def _forced_update(self):
        if time.time() - self.last_forced_update < 600:
            return
        self.last_forced_update = time.time()
        if self.updater():
            self.data["last_update_check"] = time.time()
            self.store.save()

    def toggle(self):
        with self.lock:
            if not self.has_queue():
                query = (self.data.get("query") or "").strip()
                if query.startswith("spotify:"):
                    threading.Thread(
                        target=self.play_spotify,
                        args=(query[8:], self.data.get("playlist_title") or ""),
                        daemon=True,
                    ).start()
                elif query:
                    threading.Thread(
                        target=self.search_and_play, args=(query,), daemon=True
                    ).start()
                else:
                    threading.Thread(target=self.ask_search, daemon=True).start()
                return
            if self.status == "playing":
                self.pause()
                return
            if self.status == "paused":
                if self.cur is None or self.cur.stale():
                    self.start_track(self.data["index"], self.last_pos)
                else:
                    try:
                        self.player.pause = False
                    except Exception:
                        self.start_track(self.data["index"], self.last_pos)
                        return
                    self.status = "playing"
                    self.progress_at = time.time()
                    self._changed()
                return
            if self.status == "idle":
                self.start_track(self.data["index"], float(self.data["position"] or 0))

    def pause(self):
        with self.lock:
            if self.status != "playing":
                return
            try:
                self.player.pause = True
            except Exception:
                log.exception("pause failed")
            self.status = "paused"
            self._remember_position()
            self.store.save()
            self._changed()

    def next(self):
        with self.lock:
            if self.has_queue():
                self.skips = 0
                self._step(1)

    def previous(self):
        with self.lock:
            if self.has_queue():
                self.skips = 0
                self._step(-1)

    def _step(self, delta):
        order = self.data["order"]
        leaving = self.current_entry()
        if leaving and self.cur is not None:
            with self.cache_lock:
                self.cache[leaving["id"]] = self.cur
        self.data["index"] = (self.data["index"] + delta) % len(order)
        self.data["position"] = 0.0
        self.start_track(self.data["index"], 0.0, True)

    def set_shuffle(self, on):
        with self.lock:
            self.data["shuffle"] = bool(on)
            entry = self.current_entry()
            if entry:
                if on:
                    rest = [e["id"] for e in self.data["orig"] if e["id"] != entry["id"]]
                    random.shuffle(rest)
                    self.data["order"] = [entry["id"]] + rest
                    self.data["index"] = 0
                else:
                    self.data["order"] = [e["id"] for e in self.data["orig"]]
                    self.data["index"] = self.data["order"].index(entry["id"])
            self.store.save()
            self._changed()

    def play_spotify(self, pid, name=""):
        with self.lock:
            self.gen += 1
            gen = self.gen
            self.status = "loading"
            self.title = f"Loading: {name or 'Spotify playlist'}"
            self._changed()
        try:
            title, entries = self.spotify_loader(pid)
        except Exception as exc:
            log.warning("spotify load failed: %s", exc)
            with self.lock:
                if gen != self.gen:
                    return
                self.title = ""
                self.status = "paused" if self.cur is not None else "idle"
                self._changed()
            self.on_notify(f"Spotify playlist failed: {exc}")
            return
        with self.lock:
            if gen != self.gen:
                return
            self.set_queue(f"spotify:{pid}", pid, title, entries)
            self.start_track(0, 0.0)

    def resolve_entry(self, entry):
        video_id = entry.get("yt")
        if not video_id and entry.get("q"):
            video_id = self.finder(entry["q"], entry.get("dur"))
            entry["yt"] = video_id
        return self.resolver(video_id or entry["id"])

    def search_and_play(self, query):
        query = (query or "").strip()
        if not query:
            return
        with self.lock:
            self.gen += 1
            gen = self.gen
            self.status = "loading"
            self.title = f"Searching: {query}"
            self._changed()
        try:
            result = self.searcher(query)
        except Exception as exc:
            log.warning("search failed: %s", exc)
            with self.lock:
                if gen != self.gen:
                    return
                self.title = ""
                self.status = "idle"
                if self.cur is not None:
                    self.status = "paused"
                self._changed()
            self.on_notify(f"Search failed: {exc}")
            return
        with self.lock:
            if gen != self.gen:
                return
            self.set_queue(query, result["pid"], result["title"], result["entries"])
            self.start_track(0, 0.0)

    def set_queue(self, query, pid, title, entries):
        self.data["query"] = query
        self.data["playlist_id"] = pid
        self.data["playlist_title"] = title
        self.data["orig"] = list(entries)
        order = [e["id"] for e in entries]
        if self.data["shuffle"]:
            random.shuffle(order)
        self.data["order"] = order
        self.data["index"] = 0
        self.data["position"] = 0.0
        self.skips = 0
        self.store.save()

    def start_track(self, index, position, use_cache=False):
        with self.lock:
            self.gen += 1
            gen = self.gen
            self.data["index"] = index
            self.status = "loading"
            entry = self.current_entry()
            self.title = entry["title"] if entry else ""
            self.last_pos = position
            self._changed()
        threading.Thread(
            target=self._load, args=(gen, index, position, use_cache), daemon=True
        ).start()

    def cache_take(self, video_id):
        with self.cache_lock:
            res = self.cache.get(video_id)
        if res is not None and res.stale(300):
            with self.cache_lock:
                self.cache.pop(video_id, None)
            return None
        return res

    def _prefetch_loop(self):
        while not self.stop_flag.is_set():
            self.prefetch_event.wait(1.0)
            if self.stop_flag.is_set():
                return
            if not self.prefetch_event.is_set():
                continue
            self.prefetch_event.clear()
            try:
                self._prefetch_pass()
            except Exception:
                log.exception("prefetch error")

    def _prefetch_pass(self):
        with self.lock:
            order = list(self.data["order"])
            index = self.data["index"]
            status = self.status
        if status not in ("playing", "paused", "loading") or len(order) < 2:
            return
        wanted = []
        for delta in PREFETCH_OFFSETS:
            vid = order[(index + delta) % len(order)]
            if vid != order[index % len(order)] and vid not in wanted:
                wanted.append(vid)
        with self.cache_lock:
            for key in list(self.cache):
                if key not in wanted:
                    self.cache.pop(key, None)
        for vid in wanted:
            if self.stop_flag.is_set() or self.prefetch_event.is_set():
                return
            while self.status == "loading" and not self.stop_flag.is_set():
                time.sleep(0.5)
            if self.cache_take(vid) is not None:
                continue
            if time.time() - self.failed.get(vid, 0) < 600:
                continue
            try:
                found = self.entry_for(vid)
                res = self.resolve_entry(found) if found else self.resolver(vid)
            except Exception as exc:
                self.failed[vid] = time.time()
                log.info("prefetch skipped %s: %s", vid, exc)
                continue
            with self.cache_lock:
                self.cache[vid] = res
            log.info("prefetched %s", vid)

    def _load(self, gen, index, position, use_cache=False):
        entry = self.current_entry()
        if not entry:
            return
        resolved = self.cache_take(entry["id"]) if use_cache else None
        error = None
        for attempt in range(0 if resolved else 3):
            if gen != self.gen or self.stop_flag.is_set():
                return
            try:
                resolved = self.resolve_entry(entry)
                break
            except Exception as exc:
                error = exc
                log.warning("resolve failed (%s): %s", attempt + 1, exc)
                if attempt == 1:
                    self._forced_update()
                time.sleep(1.5 * (attempt + 1))
        with self.lock:
            if gen != self.gen or self.stop_flag.is_set():
                return
            if resolved is None:
                self._give_up_on_track(error)
                return
            self.cur = resolved
            if resolved.title and not entry.get("q"):
                self.title = resolved.title
            try:
                self.player.pause = False
                self.player["start"] = f"{position:.2f}" if position > 1 else "none"
                if resolved.user_agent:
                    self.player["user-agent"] = resolved.user_agent
                extra = [
                    f"{k}: {v}"
                    for k, v in resolved.headers.items()
                    if k.lower() not in ("user-agent", "range")
                ]
                try:
                    self.player["http-header-fields"] = extra
                except Exception:
                    log.warning("could not set http headers")
                self.player.play(resolved.url)
            except Exception as exc:
                log.exception("mpv play failed")
                self._give_up_on_track(exc)
                return
            self.status = "playing"
            self.started_at = time.time()
            self.seen_active = False
            self.progress_at = time.time()
            self.progress_pos = None
            self.last_pos = position
            self.resume_from = position
            self.last_dur = resolved.duration
            self._changed()
            self.prefetch_event.set()
            if entry["id"] != self.last_announced:
                self.last_announced = entry["id"]
                try:
                    self.on_track(
                        entry.get("yt") or entry["id"], self.title or entry["title"]
                    )
                except Exception:
                    log.exception("track announcement failed")

    def _give_up_on_track(self, error):
        self.skips += 1
        order = self.data["order"]
        if self.skips > MAX_SKIPS or len(order) <= 1:
            self.status = "paused" if self.cur is not None else "idle"
            self.title = ""
            self._changed()
            self.on_notify(f"Playback failed: {error}")
            return
        self.on_notify(f"Skipping track: {error}")
        self._step(1)

    def _recover(self, reason):
        with self.lock:
            if self.status not in ("playing",):
                return
            self.recoveries += 1
            log.warning("recovering (%s), attempt %s", reason, self.recoveries)
            current = self.current_entry()
            if current:
                with self.cache_lock:
                    self.cache.pop(current["id"], None)
            if self.recoveries > MAX_RECOVERIES:
                self.recoveries = 0
                self._give_up_on_track(reason)
                return
            if self.recoveries == 2:
                threading.Thread(target=self._forced_update, daemon=True).start()
            self.start_track(self.data["index"], max(0.0, self.last_pos - 1.0))

    def _remember_position(self):
        self.data["position"] = float(self.last_pos or 0.0)

    def _poll(self):
        player = self.player
        try:
            idle = bool(player.idle_active)
            pos = player.time_pos
            dur = player.duration
            core_idle = bool(player.core_idle)
            paused = bool(player.pause)
        except Exception:
            return None
        return idle, pos, dur, core_idle, paused

    def _watchdog(self):
        while not self.stop_flag.wait(0.5):
            try:
                self._tick()
            except Exception:
                log.exception("watchdog error")

    def _tick(self):
        with self.lock:
            status = self.status
            if status in ("idle", "loading"):
                return
            snap = self._poll()
            if snap is None:
                return
            idle, pos, dur, core_idle, paused = snap
            now = time.time()
            if pos is not None:
                self.last_pos = float(pos)
            if dur:
                self.last_dur = float(dur)
            if status == "paused":
                if now - self.last_save > SAVE_INTERVAL:
                    self._remember_position()
                    self.store.save()
                    self.last_save = now
                return
            if not idle:
                self.seen_active = True
            if idle:
                if self.seen_active:
                    ended = self.last_dur and self.last_pos >= self.last_dur - 3.0
                    if ended or (not self.last_dur and self.last_pos > 1):
                        self.recoveries = 0
                        self.skips = 0
                        self.seen_active = False
                        self.data["position"] = 0.0
                        self._step(1)
                    else:
                        self.seen_active = False
                        self._recover("stream ended early")
                elif now - self.started_at > LOAD_GRACE_SECONDS:
                    self._recover("load timeout")
                return
            if paused:
                return
            if pos is not None and (
                self.progress_pos is None or abs(pos - self.progress_pos) > 0.2
            ):
                self.progress_pos = pos
                self.progress_at = now
                if pos - self.resume_from > 10:
                    self.recoveries = 0
            elif now - self.progress_at > STALL_SECONDS:
                self.progress_at = now
                self._recover("stalled")
                return
            if now - self.last_save > SAVE_INTERVAL:
                self._remember_position()
                self.store.save()
                self.last_save = now

    def shutdown(self):
        self.stop_flag.set()
        with self.lock:
            self._remember_position()
            self.store.save()
            try:
                self.player.terminate()
            except Exception:
                pass


def dialog_main(initial, prompt="Search YouTube playlists:", button="Play"):
    import tkinter as tk

    result = {"value": ""}
    root = tk.Tk()
    root.title("yt-tray")
    root.resizable(False, False)
    tk.Label(root, text=prompt).pack(padx=14, pady=(14, 4), anchor="w")
    var = tk.StringVar(value=initial)
    entry = tk.Entry(root, textvariable=var, width=44)
    entry.pack(padx=14, pady=4)
    entry.select_range(0, "end")

    def accept(_event=None):
        result["value"] = var.get().strip()
        root.destroy()

    def cancel(_event=None):
        root.destroy()

    row = tk.Frame(root)
    row.pack(padx=14, pady=(8, 14), anchor="e")
    tk.Button(row, text="Cancel", width=8, command=cancel).pack(side="left", padx=4)
    tk.Button(row, text=button, width=8, command=accept).pack(side="left")
    root.bind("<Return>", accept)
    root.bind("<Escape>", cancel)
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{x}+{y}")
    root.attributes("-topmost", True)
    root.after(50, lambda: (root.focus_force(), entry.focus_force()))
    root.mainloop()
    sys.stdout.write(result["value"])
    sys.stdout.flush()


def popup_main_mac(title, thumb_url, seconds):
    import AppKit
    import Foundation
    from PyObjCTools import AppHelper

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    data = None
    try:
        req = urllib.request.Request(thumb_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = resp.read()
    except Exception:
        data = None
    width, height = 372, 96
    frame = AppKit.NSScreen.mainScreen().visibleFrame()
    x = frame.origin.x + frame.size.width - width - 20
    y = frame.origin.y + frame.size.height - height - 12
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        Foundation.NSMakeRect(x, y, width, height),
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    win.setLevel_(AppKit.NSStatusWindowLevel)
    win.setOpaque_(False)
    win.setBackgroundColor_(AppKit.NSColor.clearColor())
    win.setHasShadow_(True)
    win.setIgnoresMouseEvents_(True)
    win.setReleasedWhenClosed_(False)
    content = win.contentView()
    content.setWantsLayer_(True)
    layer = content.layer()
    layer.setBackgroundColor_(
        AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.13, 0.96).CGColor()
    )
    layer.setCornerRadius_(12)
    text_x = 16
    if data:
        image = AppKit.NSImage.alloc().initWithData_(
            Foundation.NSData.dataWithBytes_length_(data, len(data))
        )
        if image is not None:
            view = AppKit.NSImageView.alloc().initWithFrame_(
                Foundation.NSMakeRect(12, 12, 128, 72)
            )
            view.setImage_(image)
            view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            view.setWantsLayer_(True)
            view.layer().setCornerRadius_(6)
            view.layer().setMasksToBounds_(True)
            content.addSubview_(view)
            text_x = 152
    caption = AppKit.NSTextField.labelWithString_("Now playing")
    caption.setFont_(AppKit.NSFont.systemFontOfSize_(11))
    caption.setTextColor_(AppKit.NSColor.lightGrayColor())
    caption.setFrame_(Foundation.NSMakeRect(text_x, 68, width - text_x - 12, 16))
    content.addSubview_(caption)
    shown = title if len(title) <= 66 else title[:63] + "..."
    label = AppKit.NSTextField.wrappingLabelWithString_(shown)
    label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13))
    label.setTextColor_(AppKit.NSColor.whiteColor())
    label.setFrame_(Foundation.NSMakeRect(text_x, 12, width - text_x - 12, 54))
    content.addSubview_(label)
    win.orderFrontRegardless()
    AppHelper.callLater(seconds, app.terminate_, None)
    app.run()


def popup_main(title, thumb_url, seconds):
    if sys.platform == "darwin":
        popup_main_mac(title, thumb_url, seconds)
        return
    import base64
    import io
    import tkinter as tk

    png = None
    try:
        from PIL import Image

        req = urllib.request.Request(thumb_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((128, 72), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        png = base64.b64encode(buf.getvalue())
    except Exception:
        png = None
    width, height = 372, 96
    bg, fg, dim = "#202124", "#f1f3f4", "#9aa0a6"
    root = tk.Tk()
    if sys.platform == "darwin":
        try:
            root.tk.call("::tk::unsupported::MacWindowStyle", "style", root._w, "plain", "none")
        except Exception:
            pass
    else:
        root.withdraw()
        root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=bg)
    frame = tk.Frame(root, bg=bg, highlightthickness=1, highlightbackground="#3c4043")
    frame.pack(fill="both", expand=True)
    if png:
        photo = tk.PhotoImage(data=png)
        tk.Label(frame, image=photo, bg=bg, bd=0).pack(side="left", padx=(12, 10), pady=12)
        frame.photo = photo
    text = tk.Frame(frame, bg=bg)
    text.pack(side="left", fill="both", expand=True, padx=(0, 12), pady=10)
    tk.Label(text, text="Now playing", bg=bg, fg=dim, font=("TkDefaultFont", 9), anchor="w").pack(fill="x")
    tk.Label(
        text,
        text=title if len(title) <= 66 else title[:63] + "...",
        bg=bg,
        fg=fg,
        font=("TkDefaultFont", 11, "bold"),
        anchor="nw",
        justify="left",
        wraplength=210 if png else 330,
    ).pack(fill="both", expand=True)
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    x = sw - width - 20
    y = 40 if sys.platform == "darwin" else sh - height - 70
    root.geometry(f"{width}x{height}+{x}+{y}")
    if sys.platform != "darwin":
        root.deiconify()
    root.lift()
    root.update()
    root.bind("<Button-1>", lambda e: root.destroy())
    frame.bind("<Button-1>", lambda e: root.destroy())
    root.after(int(seconds * 1000), root.destroy)
    root.mainloop()


def ask_query_mac(initial, prompt, button):
    script = (
        f"text returned of (display dialog {json.dumps(prompt)} "
        f"default answer {json.dumps(initial)} with title \"yt-tray\" "
        f"buttons {{\"Cancel\", {json.dumps(button)}}} default button {json.dumps(button)})"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=3600
        )
    except Exception:
        log.exception("search dialog failed")
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def ask_query(initial="", prompt="Search YouTube playlists:", button="Play"):
    if sys.platform == "darwin":
        return ask_query_mac(initial, prompt, button)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--dialog",
        f"--text={initial}",
        f"--prompt={prompt}",
        f"--button={button}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            **subprocess_flags(),
        )
    except Exception:
        log.exception("search dialog failed")
        return ""
    if proc.returncode != 0:
        log.warning("search dialog error: %s", (proc.stderr or "").strip()[-300:])
        return ""
    return proc.stdout.strip()


ICON_COLORS = {
    "playing": (46, 184, 92),
    "paused": (240, 170, 30),
    "idle": (112, 118, 130),
    "loading": (60, 140, 230),
}


THEMES = (
    ("auto", "Auto (follow system)"),
    ("color", "Color"),
    ("light", "Black icon (for light bar)"),
    ("dark", "White icon (for dark bar)"),
)


def detect_system_dark():
    try:
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            winreg.CloseKey(key)
            return value == 0
        if sys.platform == "darwin":
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return out.stdout.strip().lower() == "dark"
        for key in ("color-scheme", "gtk-theme"):
            out = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", key],
                capture_output=True,
                text=True,
                timeout=3,
            )
            text = out.stdout.strip().lower()
            if out.returncode == 0 and text:
                if "dark" in text:
                    return True
                if key == "color-scheme" and ("light" in text or "default" in text):
                    continue
                if key == "gtk-theme":
                    return False
    except Exception:
        pass
    return True


def make_icon_image(kind, theme="color", frame=0):
    from PIL import Image, ImageDraw

    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if theme == "color":
        d.ellipse((6, 6, size - 6, size - 6), fill=ICON_COLORS[kind] + (255,))
        ink = (255, 255, 255, 255)
        soft = (225, 228, 235, 255)
        tri = [(94, 66), (94, 190), (198, 128)]
        bars = ((82, 68, 114, 188), (142, 68, 174, 188))
        dots = (78, 118, 158)
        dot_size = 30
    else:
        ink = (0, 0, 0, 255) if theme == "light" else (255, 255, 255, 255)
        soft = ink
        tri = [(52, 24), (52, 232), (232, 128)]
        bars = ((44, 28, 100, 228), (156, 28, 212, 228))
        dots = (24, 100, 176)
        dot_size = 56
    if kind == "paused":
        for box in bars:
            d.rounded_rectangle(box, radius=8, fill=ink)
    elif kind == "loading":
        for i, x in enumerate(dots):
            active = i == frame % 3
            sz = int(dot_size * (1.4 if active else 0.8))
            cx = x + dot_size // 2
            col = ink if active else ink[:3] + (110,)
            d.ellipse((cx - sz // 2, 128 - sz // 2, cx + sz // 2, 128 + sz // 2), fill=col)
    elif kind == "idle" and theme != "color":
        d.polygon(tri, outline=ink, width=18)
    else:
        d.polygon(tri, fill=ink if kind == "playing" else soft)
    return img.resize((64, 64), Image.LANCZOS)


SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="Activate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="Scroll"><arg type="i" name="delta" direction="in"/><arg type="s" name="orientation" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s" name="status"/></signal>
  </interface>
</node>
"""

DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"


class SniIcon:
    def __init__(self, name, image, title, menu, on_activate):
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self.Gio = Gio
        self.GLib = GLib
        self.name = name
        self.title_text = title
        self.menu = menu
        self.on_activate = on_activate
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = self.conn.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        if not reply.unpack()[0]:
            raise RuntimeError("no StatusNotifierWatcher")
        self.lock = threading.Lock()
        self.pixmap = self.to_pixmap(image)
        self.nodes = {}
        self.tree = None
        self.revision = 1
        self.signature = None
        self.loop = None
        self.rebuild(emit=False)

    def to_pixmap(self, image):
        img = image.convert("RGBA")
        r, g, b, a = img.split()
        from PIL import Image

        data = Image.merge("RGBA", (a, r, g, b)).tobytes()
        return [(img.width, img.height, data)]

    def prop(self, name):
        GLib = self.GLib
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", self.name)
        if name == "Title":
            return GLib.Variant("s", self.title_text)
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "WindowId":
            return GLib.Variant("i", 0)
        if name in ("IconName", "OverlayIconName", "AttentionIconName", "AttentionMovieName"):
            return GLib.Variant("s", "")
        if name == "IconPixmap":
            return GLib.Variant("a(iiay)", self.pixmap)
        if name in ("OverlayIconPixmap", "AttentionIconPixmap"):
            return GLib.Variant("a(iiay)", [])
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", ("", [], self.title_text, ""))
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        return None

    def menu_prop(self, name):
        GLib = self.GLib
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "TextDirection":
            return GLib.Variant("s", "ltr")
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    def build_tree(self):
        GLib = self.GLib
        import pystray

        nodes = {}
        counter = [0]

        def make(menu):
            children = []
            for item in menu:
                counter[0] += 1
                node_id = counter[0]
                if item is pystray.Menu.SEPARATOR:
                    props = {"type": GLib.Variant("s", "separator")}
                    kids = []
                else:
                    label = str(item.text).replace("_", "__")
                    props = {
                        "label": GLib.Variant("s", label),
                        "enabled": GLib.Variant("b", bool(item.enabled)),
                        "visible": GLib.Variant("b", True),
                    }
                    checked = item.checked
                    if checked is not None:
                        props["toggle-type"] = GLib.Variant(
                            "s", "radio" if item.radio else "checkmark"
                        )
                        props["toggle-state"] = GLib.Variant("i", 1 if checked else 0)
                    sub = item.submenu
                    kids = []
                    if sub is not None:
                        props["children-display"] = GLib.Variant("s", "submenu")
                        kids = make(sub)
                nodes[node_id] = (item, props)
                children.append((node_id, props, kids))
            return children

        top = make(self.menu) if self.menu is not None else []
        return nodes, (0, {"children-display": GLib.Variant("s", "submenu")}, top)

    def signature_of(self, tree):
        def sig(node):
            node_id, props, kids = node
            return (
                node_id,
                tuple(sorted((k, v.print_(False)) for k, v in props.items())),
                tuple(sig(k) for k in kids),
            )

        return sig(tree)

    def rebuild(self, emit=True):
        try:
            nodes, tree = self.build_tree()
        except Exception:
            log.exception("sni menu build failed")
            return
        signature = self.signature_of(tree)
        with self.lock:
            changed = signature != self.signature
            self.nodes = nodes
            self.tree = tree
            self.signature = signature
            if changed:
                self.revision += 1
            revision = self.revision
        if emit and changed:
            self.emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                      self.GLib.Variant("(ui)", (revision, 0)))

    def emit(self, path, iface, signal, params=None):
        try:
            self.conn.emit_signal(None, path, iface, signal, params)
        except Exception:
            log.exception("sni emit failed")

    def node_variant(self, node):
        GLib = self.GLib
        node_id, props, kids = node
        return (
            node_id,
            props,
            [GLib.Variant("(ia{sv}av)", self.node_variant(k)) for k in kids],
        )

    def find(self, node, wanted):
        if node[0] == wanted:
            return node
        for kid in node[2]:
            found = self.find(kid, wanted)
            if found:
                return found
        return None

    def flatten(self, node, out):
        out.append((node[0], node[1]))
        for kid in node[2]:
            self.flatten(kid, out)

    def on_sni_call(self, conn, sender, path, iface, method, params, invocation):
        log.info("tray call: %s", method)
        if method in ("Activate", "SecondaryActivate"):
            threading.Thread(target=self.on_activate, daemon=True).start()
        invocation.return_value(None)

    def on_sni_get(self, conn, sender, path, iface, name):
        return self.prop(name)

    def on_menu_get(self, conn, sender, path, iface, name):
        return self.menu_prop(name)

    def on_menu_call(self, conn, sender, path, iface, method, params, invocation):
        GLib = self.GLib
        try:
            if method == "GetLayout":
                parent = params.unpack()[0]
                with self.lock:
                    tree = self.tree
                    revision = self.revision
                node = self.find(tree, parent) or tree
                invocation.return_value(
                    GLib.Variant("(u(ia{sv}av))", (revision, self.node_variant(node)))
                )
            elif method == "GetGroupProperties":
                ids = params.unpack()[0]
                flat = []
                with self.lock:
                    self.flatten(self.tree, flat)
                if ids:
                    flat = [f for f in flat if f[0] in ids]
                invocation.return_value(GLib.Variant("(a(ia{sv}))", (flat,)))
            elif method == "GetProperty":
                wanted, name = params.unpack()
                flat = []
                with self.lock:
                    self.flatten(self.tree, flat)
                value = next((p.get(name) for i, p in flat if i == wanted), None)
                if value is None:
                    value = GLib.Variant("s", "")
                invocation.return_value(GLib.Variant("(v)", (value,)))
            elif method == "Event":
                node_id, event = params.unpack()[:2]
                self.fire(node_id, event)
                invocation.return_value(None)
            elif method == "EventGroup":
                for node_id, event, _, _ in params.unpack()[0]:
                    self.fire(node_id, event)
                invocation.return_value(GLib.Variant("(ai)", ([],)))
            elif method == "AboutToShow":
                if params.unpack()[0] == 0:
                    self.rebuild()
                invocation.return_value(GLib.Variant("(b)", (False,)))
            elif method == "AboutToShowGroup":
                invocation.return_value(GLib.Variant("(aiai)", ([], [])))
            else:
                invocation.return_value(None)
        except Exception:
            log.exception("sni menu call failed")
            invocation.return_value(None)

    def fire(self, node_id, event):
        if event != "clicked":
            return
        with self.lock:
            entry = self.nodes.get(node_id)
        if not entry:
            return
        item = entry[0]
        if item.submenu is not None:
            return
        threading.Thread(target=item, args=(self,), daemon=True).start()

    def register(self):
        Gio = self.Gio
        sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        menu_info = Gio.DBusNodeInfo.new_for_xml(DBUSMENU_XML)
        self.conn.register_object(
            SNI_PATH, sni_info.interfaces[0], self.on_sni_call, self.on_sni_get, None
        )
        self.conn.register_object(
            MENU_PATH, menu_info.interfaces[0], self.on_menu_call, self.on_menu_get, None
        )
        self.bus_name = "org.kde.StatusNotifierItem-%d-1" % os.getpid()
        Gio.bus_own_name_on_connection(
            self.conn, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None
        )
        self.conn.call_sync(
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher",
            "RegisterStatusNotifierItem",
            self.GLib.Variant("(s)", (self.bus_name,)),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )

    @property
    def icon(self):
        return None

    @icon.setter
    def icon(self, image):
        self.pixmap = self.to_pixmap(image)
        self.GLib.idle_add(
            lambda: (self.emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewIcon"), False)[1]
        )

    @property
    def title(self):
        return self.title_text

    @title.setter
    def title(self, value):
        if value == self.title_text:
            return
        self.title_text = value
        self.GLib.idle_add(
            lambda: (
                self.emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewTitle"),
                self.emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewToolTip"),
                False,
            )[2]
        )

    @property
    def visible(self):
        return True

    @visible.setter
    def visible(self, value):
        pass

    def update_menu(self):
        self.GLib.idle_add(lambda: (self.rebuild(), False)[1])

    def notify(self, message, title=None):
        GLib = self.GLib

        def send():
            try:
                self.conn.call_sync(
                    "org.freedesktop.Notifications",
                    "/org/freedesktop/Notifications",
                    "org.freedesktop.Notifications",
                    "Notify",
                    GLib.Variant(
                        "(susssasa{sv}i)",
                        (title or self.name, 0, "", title or self.name, message, [], {}, 5000),
                    ),
                    None,
                    self.Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
            except Exception:
                pass

        threading.Thread(target=send, daemon=True).start()

    def run(self, setup=None):
        self.register()
        self.loop = self.GLib.MainLoop()
        if setup:
            threading.Thread(target=setup, args=(self,), daemon=True).start()
        self.loop.run()

    def stop(self):
        if self.loop:
            self.GLib.idle_add(lambda: (self.loop.quit(), False)[1])


class App:
    def __init__(self):
        import pystray

        self.pystray = pystray
        self.store = Store()
        self.dialog_lock = threading.Lock()
        self.images = {}
        self.frame = 0
        self.system_dark = detect_system_dark()
        self.player = make_player()
        self.ctl = Controller(
            self.store,
            self.player,
            on_change=self.refresh,
            on_notify=self.notify,
            ask_search=self.open_search,
            on_track=self.show_popup,
        )
        self.popup_proc = None
        theme = self.effective_theme()
        self.images[(theme, "idle", 0)] = make_icon_image("idle", theme)
        self.icon = None
        if sys.platform.startswith("linux") and pystray.Icon.__module__.endswith("_appindicator"):
            try:
                self.icon = SniIcon(
                    APP, self.images[(theme, "idle", 0)], APP, self.build_menu(), self.on_toggle
                )
            except Exception:
                log.info("native tray item unavailable, using indicator", exc_info=True)
                self.icon = None
        log.info("tray: %s", "native" if self.icon is not None else "indicator fallback")
        if self.icon is None:
            self.icon = pystray.Icon(APP, self.images[(theme, "idle", 0)], APP, self.build_menu())
        threading.Thread(target=self.animate, daemon=True).start()

    def build_menu(self):
        ps = self.pystray
        return ps.Menu(
            ps.MenuItem(
                lambda item: "Pause" if self.ctl.status == "playing" else "Play",
                self.on_toggle,
                default=True,
            ),
            ps.MenuItem("Search...", self.on_search),
            ps.MenuItem("Spotify", ps.Menu(self.spotify_items)),
            ps.MenuItem("Next", self.on_next),
            ps.MenuItem("Previous", self.on_previous),
            ps.Menu.SEPARATOR,
            ps.MenuItem(
                lambda item: self.now_playing_text(), lambda icon, item: None, enabled=False
            ),
            ps.MenuItem(
                "Shuffle",
                self.on_shuffle,
                checked=lambda item: self.ctl.shuffle,
            ),
            ps.MenuItem(
                "Notifications",
                ps.Menu(
                    ps.MenuItem(
                        "On",
                        self.make_notify_action(True),
                        checked=lambda item: bool(self.store.data.get("notifications", True)),
                        radio=True,
                    ),
                    ps.MenuItem(
                        "Off",
                        self.make_notify_action(False),
                        checked=lambda item: not self.store.data.get("notifications", True),
                        radio=True,
                    ),
                ),
            ),
            ps.MenuItem(
                "Icon style",
                ps.Menu(
                    *[
                        ps.MenuItem(
                            label,
                            self.make_theme_action(key),
                            checked=lambda item, k=key: self.current_theme() == k,
                            radio=True,
                        )
                        for key, label in THEMES
                    ]
                ),
            ),
            ps.Menu.SEPARATOR,
            ps.MenuItem("Quit", self.on_quit),
        )

    def spotify_items(self):
        ps = self.pystray
        yield ps.MenuItem("Load Playlist", self.on_spotify_add)
        lists = self.store.data.get("spotify") or []
        yield ps.Menu.SEPARATOR
        if not lists:
            yield ps.MenuItem("No playlists yet", lambda icon, item: None, enabled=False)
            return
        for entry in lists:
            yield ps.MenuItem(
                self.menu_label(self.spotify_label(entry)),
                self.make_spotify_play(entry["id"], entry["name"]),
            )
        yield ps.Menu.SEPARATOR
        yield ps.MenuItem(
            "Remove",
            ps.Menu(
                lambda: (
                    ps.MenuItem(
                        self.menu_label(self.spotify_label(e)), self.make_spotify_remove(e["id"])
                    )
                    for e in list(self.store.data.get("spotify") or [])
                )
            ),
        )

    def spotify_label(self, entry):
        name = (entry.get("name") or "").strip()
        if any(ch.isalnum() for ch in name):
            return name
        count = entry.get("count")
        suffix = f"{count} tracks" if count else f"playlist {entry['id'][:4]}"
        return f"{name} ({suffix})" if name else f"Spotify {suffix}"

    def menu_label(self, text):
        text = text.replace("&", "&&")
        return text if len(text) <= 48 else text[:45] + "..."

    def make_spotify_play(self, pid, name):
        def action(icon=None, item=None):
            threading.Thread(
                target=self.ctl.play_spotify, args=(pid, name), daemon=True
            ).start()

        return action

    def make_spotify_remove(self, pid):
        def action(icon=None, item=None):
            self.store.data["spotify"] = [
                e for e in self.store.data.get("spotify", []) if e["id"] != pid
            ]
            self.store.save()
            self.refresh()

        return action

    def on_spotify_add(self, icon=None, item=None):
        threading.Thread(target=self.add_spotify, daemon=True).start()

    def add_spotify(self):
        if not self.dialog_lock.acquire(blocking=False):
            return
        try:
            text = ask_query("", "Spotify playlist URL:", "Load")
        finally:
            self.dialog_lock.release()
        if not text:
            return
        pid = spotify_playlist_id(text)
        if not pid:
            self.notify("That does not look like a Spotify playlist URL")
            return
        try:
            name, entries = fetch_spotify_playlist(pid)
        except Exception as exc:
            self.notify(f"Could not add playlist: {exc}")
            return
        lists = [e for e in self.store.data.get("spotify", []) if e["id"] != pid]
        lists.append({"id": pid, "name": name, "count": len(entries)})
        self.store.data["spotify"] = lists
        self.store.save()
        self.refresh()
        self.notify(f"Added {name} ({len(entries)} tracks)")

    def show_popup(self, video_id, title):
        if not self.store.data.get("notifications", True):
            return
        if self.popup_proc is not None and self.popup_proc.poll() is None:
            self.popup_proc.terminate()
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--popup",
            f"--text={title}",
            f"--thumb-url=https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        ]
        try:
            self.popup_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=open(config_dir() / "popup.log", "a"),
                **subprocess_flags(),
            )
        except Exception:
            log.exception("popup failed")

    def make_notify_action(self, on):
        def action(icon=None, item=None):
            self.store.data["notifications"] = on
            self.store.save()

        return action

    def make_theme_action(self, key):
        def action(icon=None, item=None):
            self.store.data["theme"] = key
            if key == "auto":
                self.system_dark = detect_system_dark()
            self.store.save()
            self.refresh()

        return action

    def now_playing_text(self):
        title = self.ctl.now_title()
        if not title:
            return "Now Playing: nothing"
        title = title.replace("&", "&&")
        if len(title) > 60:
            title = title[:57] + "..."
        return f"Now Playing: {title}"

    def tooltip(self):
        title = self.ctl.now_title()
        return f"{APP}: {title}"[:120] if title else APP

    def refresh(self):
        status = self.ctl.status
        kind = status if status in ICON_COLORS else "idle"
        try:
            self.set_icon(kind, self.frame)
            self.icon.title = self.tooltip()
            self.icon.update_menu()
        except Exception:
            log.exception("icon refresh failed")

    def current_theme(self):
        theme = self.store.data.get("theme", "auto")
        return theme if theme in dict(THEMES) else "auto"

    def effective_theme(self):
        theme = self.current_theme()
        if theme == "auto":
            return "dark" if self.system_dark else "light"
        return theme

    def current_kind(self):
        status = self.ctl.status
        return status if status in ICON_COLORS else "idle"

    def set_icon(self, kind, frame=0):
        theme = self.effective_theme()
        key = (theme, kind, frame if kind == "loading" else 0)
        if key not in self.images:
            self.images[key] = make_icon_image(kind, theme, key[2])
        self.icon.icon = self.images[key]

    def animate(self):
        ticks = 0
        while not self.ctl.stop_flag.wait(0.3):
            ticks += 1
            if ticks % 10 == 0 and self.current_theme() == "auto":
                dark = detect_system_dark()
                if dark != self.system_dark:
                    self.system_dark = dark
                    try:
                        self.set_icon(self.current_kind(), self.frame)
                    except Exception:
                        log.exception("theme switch failed")
            if self.ctl.status != "loading":
                self.frame = 0
                continue
            self.frame = (self.frame + 1) % 3
            try:
                self.set_icon("loading", self.frame)
            except Exception:
                log.exception("animation failed")

    def notify(self, message):
        log.info("notify: %s", message)

    def open_search(self):
        if not self.dialog_lock.acquire(blocking=False):
            return
        try:
            last = self.store.data.get("query", "")
            query = ask_query("" if last.startswith("spotify:") else last)
        finally:
            self.dialog_lock.release()
        if query:
            self.ctl.search_and_play(query)

    def on_toggle(self, icon=None, item=None):
        self.ctl.toggle()

    def on_search(self, icon=None, item=None):
        threading.Thread(target=self.open_search, daemon=True).start()

    def on_next(self, icon=None, item=None):
        self.ctl.next()

    def on_previous(self, icon=None, item=None):
        self.ctl.previous()

    def on_shuffle(self, icon=None, item=None):
        self.ctl.set_shuffle(not self.ctl.shuffle)

    def on_quit(self, icon=None, item=None):
        self.ctl.shutdown()
        self.icon.stop()

    def install_mac_click(self):
        import AppKit
        import objc
        from Foundation import NSObject

        app = self

        class ClickHandler(NSObject):
            def clicked_(self, sender):
                event = AppKit.NSApp.currentEvent()
                right = event is not None and (
                    event.type() == AppKit.NSEventTypeRightMouseUp
                    or bool(event.modifierFlags() & AppKit.NSEventModifierFlagControl)
                )
                if right:
                    handle = app.icon._menu_handle
                    if handle:
                        item = app.icon._status_item
                        item.setMenu_(handle[0])
                        item.button().performClick_(None)
                        item.setMenu_(None)
                else:
                    app.on_toggle()

        icon = self.icon

        def update_menu():
            callbacks = []
            nsmenu = icon._create_menu(icon.menu, callbacks)
            icon._menu_handle = (nsmenu, callbacks) if nsmenu else None

        icon._update_menu = update_menu
        self.click_handler = ClickHandler.alloc().init()

        def apply():
            try:
                item = icon._status_item
                item.setMenu_(None)
                button = item.button()
                button.setTarget_(self.click_handler)
                button.setAction_(
                    objc.selector(None, selector=b"clicked:", signature=b"v@:@")
                )
                button.sendActionOn_(
                    AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp
                )
                update_menu()
            except Exception:
                log.exception("mac click handling failed")

        from PyObjCTools import AppHelper

        AppHelper.callAfter(apply)

    def run(self):
        def setup(icon):
            if sys.platform == "darwin":
                try:
                    self.install_mac_click()
                except Exception:
                    log.exception("mac click handling failed, using default menu click")
            icon.visible = True
            self.refresh()

        self.icon.run(setup)


def new_instance_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock


def single_instance():
    sock = new_instance_socket()
    try:
        sock.bind(("127.0.0.1", INSTANCE_PORT))
        sock.listen(5)
    except OSError:
        return None
    return sock


def control_loop(sock, app):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(2)
            data = conn.recv(64).strip()
            if data == b"quit":
                conn.sendall(b"ok")
                conn.close()
                app.on_quit()
                return
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def stop_running(wait=15.0):
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2) as conn:
            conn.sendall(b"quit")
            conn.settimeout(3)
            conn.recv(8)
    except OSError:
        return False
    end = time.time() + wait
    while time.time() < end:
        probe = new_instance_socket()
        try:
            probe.bind(("127.0.0.1", INSTANCE_PORT))
            probe.close()
            return True
        except OSError:
            probe.close()
            time.sleep(0.2)
    return True


def selftest(query, seconds=8):
    print(f"yt-tray {VERSION} selftest")
    import yt_dlp

    print("yt-dlp", yt_dlp.version.__version__)
    print("searching:", query)
    try:
        result = search_playlist(query)
        print("playlist:", result["title"], f"({len(result['entries'])} items)")
        first = result["entries"][0]
        print("resolving:", first["title"])
        resolved = resolve(first["id"])
    except Exception as exc:
        print("FAIL:", exc)
        return 1
    print("stream ok, expires in", int(resolved.expire - time.time()), "s")
    try:
        player = make_player(ao="null")
    except Exception as exc:
        print("FAIL: could not start mpv:", exc)
        return 1
    player.play(resolved.url)
    start = time.time()
    moved = None
    while time.time() - start < seconds + 15:
        time.sleep(0.5)
        pos = player.time_pos
        if pos is not None and pos > 2:
            moved = pos
            break
    player.terminate()
    if moved is None:
        print("FAIL: mpv did not advance")
        return 1
    print(f"PASS: playback advanced to {moved:.1f}s")
    return 0


PIP_DEPS = {
    "yt_dlp": "yt-dlp[default,deno]",
    "mpv": "python-mpv>=1.0.6",
    "pystray": "pystray>=0.19.5",
    "PIL": "Pillow>=10.0",
}


def script_dir():
    return Path(__file__).resolve().parent


def venv_dir():
    return script_dir() / ".venv"


def venv_python():
    base = venv_dir() / ("Scripts" if sys.platform == "win32" else "bin")
    exe = base / ("python.exe" if sys.platform == "win32" else "python")
    if sys.platform == "win32" and Path(sys.executable).name.lower() == "pythonw.exe":
        w = base / "pythonw.exe"
        if w.exists():
            return w
    return exe


def in_venv():
    try:
        return Path(sys.prefix).resolve() == venv_dir().resolve()
    except OSError:
        return False


def say(msg):
    print(f"[yt-tray setup] {msg}", flush=True)
    log.info(msg)


def run_cmd(cmd, **kw):
    try:
        return subprocess.run(cmd, **subprocess_flags(), **kw).returncode == 0
    except Exception:
        return False


def have_command(name):
    import shutil

    return shutil.which(name) is not None


def have_libmpv():
    import ctypes.util

    prepare_libmpv()
    if sys.platform == "win32":
        return any(ctypes.util.find_library(n) for n in ("mpv-2.dll", "libmpv-2.dll", "mpv-1.dll"))
    return bool(ctypes.util.find_library("mpv"))


def have_module(name):
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def sudo_prefix():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    if have_command("sudo"):
        return ["sudo"]
    return None


def install_system_packages():
    need_mpv = not have_libmpv()
    marker = config_dir() / ".system-setup"
    tried = marker.exists()
    need_tk = not have_module("tkinter") and not tried
    need_gi = sys.platform.startswith("linux") and not have_module("gi") and not tried
    if not (need_mpv or need_tk or need_gi):
        return
    try:
        marker.write_text("1")
    except OSError:
        pass
    missing = [n for n, f in (("libmpv", need_mpv), ("tkinter", need_tk), ("gi", need_gi)) if f]
    say("missing system components: " + ", ".join(missing))
    if sys.platform == "darwin":
        if not have_command("brew"):
            say("Homebrew not found. Install it from https://brew.sh then run again")
            return
        pkgs = []
        if need_mpv:
            pkgs.append("mpv")
        if need_tk:
            pkgs.append("python-tk")
        run_cmd(["brew", "install"] + pkgs)
    elif sys.platform == "win32":
        if need_mpv:
            install_libmpv_windows()
        if need_tk:
            say("tkinter missing: reinstall Python from python.org with tcl/tk enabled")
    else:
        pre = sudo_prefix()
        if pre is None:
            say("sudo not found, install these packages manually: mpv libmpv tkinter python3-gi")
            return
        if have_command("apt-get"):
            run_cmd(pre + ["apt-get", "update"])
            pkgs = ["mpv", "python3-tk", "python3-gi", "gir1.2-ayatanaappindicator3-0.1"]
            if not run_cmd(pre + ["apt-get", "install", "-y", "libmpv2"] + pkgs):
                run_cmd(pre + ["apt-get", "install", "-y", "libmpv1"] + pkgs)
        elif have_command("dnf"):
            run_cmd(pre + ["dnf", "install", "-y", "mpv-libs", "python3-tkinter", "python3-gobject", "libayatana-appindicator-gtk3"])
        elif have_command("pacman"):
            run_cmd(pre + ["pacman", "-S", "--noconfirm", "mpv", "tk", "python-gobject", "libayatana-appindicator"])
        elif have_command("zypper"):
            run_cmd(pre + ["zypper", "--non-interactive", "install", "mpv", "libmpv2", "python3-tk", "python3-gobject", "typelib-1_0-AyatanaAppIndicator3-0_1"])
        else:
            say("unknown package manager, install mpv, libmpv, python3-tk and python3-gi manually")
    if not have_libmpv():
        say("libmpv is still missing, playback will not work until it is installed")


GNOME_TRAY_UUID = "appindicatorsupport@rgcjonas.gmail.com"


def ensure_gnome_tray():
    if not sys.platform.startswith("linux"):
        return
    if "gnome" not in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return
    if not have_command("gnome-extensions"):
        return

    def listing(*flags):
        try:
            out = subprocess.run(
                ["gnome-extensions", "list", *flags],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return out.stdout
        except Exception:
            return ""

    if GNOME_TRAY_UUID in listing():
        if GNOME_TRAY_UUID not in listing("--enabled"):
            run_cmd(["gnome-extensions", "enable", GNOME_TRAY_UUID])
        return
    marker = config_dir() / ".gnome-tray"
    if marker.exists():
        return
    try:
        marker.write_text("1")
    except OSError:
        pass
    pre = sudo_prefix()
    if pre is None:
        say("GNOME needs the AppIndicator extension to show the tray icon")
        return
    say("installing the GNOME AppIndicator extension for the tray icon")
    pkg = "gnome-shell-extension-appindicator"
    if have_command("dnf"):
        done = run_cmd(pre + ["dnf", "install", "-y", pkg])
    elif have_command("apt-get"):
        done = run_cmd(pre + ["apt-get", "install", "-y", pkg])
    elif have_command("pacman"):
        done = run_cmd(pre + ["pacman", "-S", "--noconfirm", pkg])
    elif have_command("zypper"):
        done = run_cmd(pre + ["zypper", "--non-interactive", "install", pkg])
    else:
        done = False
    if not done:
        say("could not install the extension, install gnome-shell-extension-appindicator manually")
        return
    run_cmd(["gnome-extensions", "enable", GNOME_TRAY_UUID])
    say("log out and back in once, then the tray icon will appear")


def extract_7z(archive, out):
    import shutil

    attempts = []
    tar = shutil.which("tar")
    if tar:
        attempts.append([tar, "-xf", str(archive), "-C", str(out)])
    for exe in ("7z", "7za", "7zz"):
        found = shutil.which(exe)
        if found:
            attempts.append([found, "x", "-y", f"-o{out}", str(archive)])
    for exe in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if os.path.exists(exe):
            attempts.append([exe, "x", "-y", f"-o{out}", str(archive)])
    for cmd in attempts:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
        except Exception:
            continue
        if result.returncode == 0 and any(out.rglob("*.dll")):
            return True
    try:
        pip = [str(venv_python()) if venv_python().exists() else sys.executable, "-m", "pip"]
        run_cmd(pip + ["install", "--quiet", "py7zr"])
        import py7zr

        with py7zr.SevenZipFile(archive) as z:
            z.extractall(out)
        return True
    except Exception:
        return False


def install_libmpv_windows():
    import platform
    import shutil
    import tempfile

    say("downloading libmpv for Windows")
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest",
            headers={"User-Agent": APP},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read().decode("utf-8"))
        arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
        asset = None
        for a in release.get("assets", []):
            name = a["name"]
            if name.startswith(f"mpv-dev-{arch}-") and "v3" not in name and name.endswith(".7z"):
                asset = a
                break
        if asset is None:
            raise RuntimeError("no mpv-dev archive found in latest release")
        tmp = Path(tempfile.mkdtemp())
        archive = tmp / asset["name"]
        urllib.request.urlretrieve(asset["browser_download_url"], archive)
        out = tmp / "x"
        out.mkdir()
        if not extract_7z(archive, out):
            raise RuntimeError("could not unpack the archive")
        dll = next(out.rglob("libmpv-2.dll"), None) or next(out.rglob("mpv-2.dll"), None)
        if dll is None:
            raise RuntimeError("libmpv dll not found in archive")
        shutil.copy2(dll, script_dir() / "mpv-2.dll")
        say("installed mpv-2.dll next to the script")
    except Exception as exc:
        say(f"automatic libmpv download failed ({exc}). See README for manual steps")


def bootstrap(setup_only=False):
    if not in_venv():
        vp = venv_python()
        if not vp.exists():
            say("creating virtual environment in .venv")
            import venv

            venv.EnvBuilder(
                with_pip=True, system_site_packages=sys.platform.startswith("linux")
            ).create(venv_dir())
        if not vp.exists():
            say("could not create virtual environment, continuing with current Python")
            return
        args = [str(vp), str(Path(__file__).resolve())] + sys.argv[1:]
        if sys.platform == "win32":
            sys.exit(subprocess.run(args).returncode)
        os.execv(str(vp), args)
    missing = [spec for mod, spec in PIP_DEPS.items() if not have_module(mod)]
    if missing:
        say("installing Python packages: " + ", ".join(missing))
        run_cmd(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
            + missing
        )
    install_system_packages()
    ensure_gnome_tray()
    if setup_only:
        ok = all(have_module(m) for m in PIP_DEPS) and have_libmpv()
        say("ready" if ok else "setup incomplete, see messages above")
        sys.exit(0 if ok else 1)


SERVICE_LABEL = "com.justinoros.yt-tray"


def service_command():
    py = venv_python() if venv_python().exists() else Path(sys.executable)
    if sys.platform == "win32" and py.name.lower() == "python.exe":
        w = py.with_name("pythonw.exe")
        if w.exists():
            py = w
    return str(py), str(Path(__file__).resolve())


def service_install():
    py, script = service_command()
    if sys.platform == "win32":
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, f'"{py}" "{script}"')
        winreg.CloseKey(key)
        print("installed: starts at login")
    elif sys.platform == "darwin":
        import plistlib

        path = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        plist = {
            "Label": SERVICE_LABEL,
            "ProgramArguments": [py, script],
            "RunAtLoad": True,
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            },
        }
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
        uid = str(os.getuid())
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
        run_cmd(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
        print("installed:", path)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        path = base / "autostart" / f"{APP}.desktop"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=yt-tray\n"
            f'Exec="{py}" "{script}"\nX-GNOME-Autostart-enabled=true\n',
            encoding="utf-8",
        )
        print("installed:", path)
    return 0


def service_remove():
    if sys.platform == "win32":
        import winreg

        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            )
            winreg.DeleteValue(key, APP)
            winreg.CloseKey(key)
        except FileNotFoundError:
            pass
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
        path.unlink(missing_ok=True)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        (base / "autostart" / f"{APP}.desktop").unlink(missing_ok=True)
    print("removed")
    return 0


def main():
    parser = argparse.ArgumentParser(prog=APP)
    parser.add_argument("--dialog", action="store_true")
    parser.add_argument("--text", default="")
    parser.add_argument("--popup", action="store_true")
    parser.add_argument("--prompt", default="Search YouTube playlists:")
    parser.add_argument("--button", default="Play")
    parser.add_argument("--thumb-url", default="")
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--selftest", metavar="QUERY")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--install-service", action="store_true")
    parser.add_argument("--remove-service", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(VERSION)
        return 0
    if args.dialog:
        dialog_main(args.text, args.prompt, args.button)
        return 0
    if args.popup:
        popup_main(args.text, args.thumb_url, args.seconds)
        return 0
    if args.stop:
        print("stopped" if stop_running() else "not running")
        return 0
    if args.restart:
        print("stopped old instance" if stop_running() else "no running instance, starting")
    if os.environ.get("YT_TRAY_NO_BOOTSTRAP") != "1":
        bootstrap(args.setup)
    elif args.setup:
        return 0
    if args.install_service:
        return service_install()
    if args.remove_service:
        return service_remove()
    if args.update:
        return 0 if update_ytdlp() else 1
    if args.selftest:
        return selftest(args.selftest)
    setup_logging()
    lock = single_instance()
    if lock is None:
        log.info("already running")
        return 0
    try:
        app = App()
        threading.Thread(target=control_loop, args=(lock, app), daemon=True).start()
        app.run()
    except Exception:
        log.exception("fatal")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
