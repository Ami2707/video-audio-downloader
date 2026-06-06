import sys
import os
import re
import logging
import platform
import subprocess
import urllib.request
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor

# ===========================================================================
#  Frozen-build bootstrap  (packaged .exe — see VidAudDownloader.spec / BUILD.md)
# ===========================================================================
# When this runs as the packaged Windows app (PyInstaller), the program lives
# in a per-user install folder and ships yt-dlp as a *loose, updatable* package
# tree under  lib\  — NOT frozen into the .exe. That's what lets the app
# refresh yt-dlp on its own (YouTube breaks stale copies within weeks) without
# the user ever touching pip or a console. A downloaded update is staged under
# lib_update\ and swapped in here, before yt_dlp is first imported, so the
# process always loads one consistent version.
#
# In a plain "python VideoAudioDownloader_UI.py" run, IS_FROZEN is False and
# none of this applies: yt-dlp is imported from the global site-packages exactly
# as before.
IS_FROZEN = getattr(sys, "frozen", False)
APP_DIR = (os.path.dirname(sys.executable) if IS_FROZEN
           else os.path.dirname(os.path.abspath(__file__)))
LIB_DIR = os.path.join(APP_DIR, "lib")             # updatable yt-dlp (frozen build)
LIB_STAGE_DIR = os.path.join(APP_DIR, "lib_update")  # a downloaded update awaiting swap-in


def _apply_staged_lib_update():
    """Swap a previously-downloaded yt-dlp update into lib\\. Called at startup
    before yt_dlp is imported, so nothing from lib\\ is in use yet and the
    replace is safe. A failure just leaves the current version in place."""
    if not os.path.isdir(LIB_STAGE_DIR):
        return
    import shutil
    try:
        for name in os.listdir(LIB_STAGE_DIR):
            src = os.path.join(LIB_STAGE_DIR, name)
            dst = os.path.join(LIB_DIR, name)
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.move(src, dst)
        shutil.rmtree(LIB_STAGE_DIR, ignore_errors=True)
    except Exception:
        pass


class _LibFirstImporter:
    """Make the loose, auto-updatable packages in lib\\ win over the copies
    frozen into the .exe — without disturbing how anything else imports.

    Why is yt-dlp frozen into the .exe at all if lib\\ overrides it? Because
    freezing it lets PyInstaller discover and bundle every *stdlib* module
    yt-dlp needs (optparse, sqlite3, …) — which the loose copy in lib\\ then
    imports at runtime. The frozen copy is also a guaranteed-working fallback if
    lib\\ is ever missing or a bad update lands there.

    This finder sits first on sys.meta_path and only claims the yt-dlp stack,
    resolving it from lib\\; every other import falls straight through to the
    normal frozen importer."""
    _roots = ("yt_dlp", "yt_dlp_ejs", "yt_dlp_plugins")

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] not in self._roots:
            return None
        from importlib.machinery import PathFinder
        return PathFinder.find_spec(
            fullname, [LIB_DIR] if path is None else path, target)


if IS_FROZEN and os.path.isdir(os.path.join(LIB_DIR, "yt_dlp")):
    _apply_staged_lib_update()
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)            # so yt-dlp's plugin scan finds lib\
    sys.meta_path.insert(0, _LibFirstImporter())

import yt_dlp
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QComboBox, QPlainTextEdit,
    QProgressBar, QFrame, QGraphicsDropShadowEffect, QSizePolicy, QDialog,
    QSplitter, QCheckBox, QMenu, QSystemTrayIcon, QColorDialog
)
from PySide6.QtCore import (
    Qt, QThread, Signal, QSettings, QTimer, QUrl,
    QPropertyAnimation, QEasingCurve, QRectF, QPointF, QSize
)
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QPen, QBrush,
    QLinearGradient, QPainterPath, QAction, QDesktopServices
)


# ===========================================================================
#  File logging
# ===========================================================================
# Everything is appended to downloader.log next to this script, including
# yt-dlp's full verbose output, so problems can be diagnosed from the file
# alone (no need to copy/paste from the window).
LOG_FILE = os.path.join(APP_DIR, "downloader.log")
VERBOSE_LOG = True  # capture yt-dlp's detailed -v output into the log file

_file_logger = logging.getLogger("vad")
_file_logger.setLevel(logging.DEBUG)
try:
    _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                       "%Y-%m-%d %H:%M:%S"))
    _file_logger.addHandler(_fh)
except Exception:
    pass  # never let logging setup crash the app


def flog(msg):
    """Write a line to the log file only."""
    try:
        _file_logger.info(msg)
    except Exception:
        pass


# ===========================================================================
#  Configuration
# ===========================================================================
#
# --- YouTube cookies ---------------------------------------------------------
# YouTube now hides high-resolution formats and throws random "Sign in to
# confirm you're not a bot" errors for anonymous downloads — without cookies
# you usually only get 360p, and downloads fail intermittently. Letting yt-dlp
# borrow cookies from a browser you're logged into fixes BOTH problems.
#
# This is the single most important setting for reliable YouTube downloads.
# Pick the browser you use for YouTube. On Windows, Firefox is the most
# reliable source (Chrome/Edge/Brave often fail with a DPAPI decrypt error
# because of Chromium's app-bound cookie encryption).
# Options: "none", "firefox", "chrome", "edge", "brave", "chromium",
#          "opera", "vivaldi"
DEFAULT_COOKIES_BROWSER = "firefox"

# --- Advanced: player clients ------------------------------------------------
# yt-dlp pulls formats from YouTube "player clients" and picks sensible
# defaults that change every release. Leave this empty to use those defaults
# (recommended). Only override if you know what you're doing — forcing the
# wrong client can cause "page needs to be reloaded" errors.
# Possible values: tv, ios, android, web, web_safari, mweb
YOUTUBE_PLAYER_CLIENTS = []

# Generic robustness settings applied to every download.
COMMON_OPTS = {
    'windowsfilenames': True,      # strip characters Windows can't put in filenames
    'restrictfilenames': False,
    'ignoreerrors': False,         # we handle errors per-URL ourselves
    'retries': 10,                 # retry the whole download on transient errors
    'fragment_retries': 10,        # retry individual DASH/HLS fragments
    'file_access_retries': 5,
    'extractor_retries': 3,
    'continuedl': True,            # resume partial downloads
    'concurrent_fragment_downloads': 4,
    'geo_bypass': True,
    'overwrites': False,
    'quiet': True,                 # silence stdout; we use a logger + hooks instead
    'no_warnings': False,
    'no_color': True,              # don't emit ANSI colour codes into our log
    'noprogress': True,            # we draw our own progress bar
    'verbose': VERBOSE_LOG,        # full detail routed to the log file (not the UI)
}


# ===========================================================================
#  PO-token provider (the fix for YouTube's SABR / "empty file" wall)
# ===========================================================================
# Modern YouTube withholds real stream URLs unless a "PO token" is supplied.
# We bundle a local provider: a portable Node.js runtime + the bgutil server,
# talked to by the pip plugin `bgutil-ytdlp-pot-provider`. The app launches
# the server automatically; yt-dlp auto-discovers it on 127.0.0.1:4416.
_SCRIPT_DIR = APP_DIR  # next to the script in dev, next to the .exe when packaged
RUNTIME_DIR = os.path.join(_SCRIPT_DIR, "runtime")
NODE_EXE = os.path.join(RUNTIME_DIR, "node", "node.exe")
POT_SERVER_JS = os.path.join(RUNTIME_DIR, "bgutil", "server", "build", "main.js")
POT_PORT = 4416

# --- ffmpeg --------------------------------------------------------------
# The packaged build ships ffmpeg/ffprobe in ffmpeg\ next to the .exe so users
# don't have to install anything. In a dev run that folder doesn't exist and we
# fall back to ffmpeg on PATH (FFMPEG_LOCATION stays None → yt-dlp uses PATH).
FFMPEG_DIR = os.path.join(_SCRIPT_DIR, "ffmpeg")
FFMPEG_LOCATION = FFMPEG_DIR if os.path.exists(
    os.path.join(FFMPEG_DIR, "ffmpeg.exe")) else None


def patch_pot_logger():
    """yt-dlp's PoT logger predates the bgutil plugin's `once=` kwarg on
    debug/trace/info/error, which would crash YouTube extraction. Wrap those
    methods to accept (and ignore) extra args. Idempotent and update-safe."""
    try:
        from yt_dlp.extractor.youtube.pot import _director as _d
        cls = _d.YoutubeIEContentProviderLogger
        if getattr(cls, "_once_shim", False):
            return
        import inspect
        for name in ("trace", "debug", "info", "error"):
            orig = cls.__dict__.get(name)
            if orig is None:
                continue
            # Skip if it already accepts a 'once' kwarg.
            try:
                if "once" in inspect.signature(orig).parameters:
                    continue
            except (TypeError, ValueError):
                pass

            def make(o):
                def wrapped(self, message, *args, **kwargs):
                    return o(self, message)
                return wrapped
            setattr(cls, name, make(orig))
        cls._once_shim = True
    except Exception:
        pass  # never let the shim crash the app


def pot_server_running():
    import socket
    try:
        with socket.socket() as s:
            s.settimeout(0.4)
            return s.connect_ex(("127.0.0.1", POT_PORT)) == 0
    except Exception:
        return False


def start_pot_server():
    """Launch the bundled PO-token server if present and not already up."""
    if pot_server_running():
        return None
    if not (os.path.exists(NODE_EXE) and os.path.exists(POT_SERVER_JS)):
        return None
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        return subprocess.Popen(
            [NODE_EXE, POT_SERVER_JS], creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        flog(f"could not start PO-token server: {e}")
        return None


# Apply the logger shim as soon as the module loads.
patch_pot_logger()


# ===========================================================================
#  Helpers
# ===========================================================================
class _Cancelled(Exception):
    """Raised inside the progress hook to abort a download cleanly."""


_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Harmless, noisy messages that look alarming but don't mean a download failed.
_NOISE = (
    "Deprecated Feature",
    "Support for Python version",
    "unsupported firefox cookies database",
    "forcing SABR streaming",
    "missing a url",
    "nsig extraction failed",
)


def _is_noise(msg):
    return any(n in msg for n in _NOISE)


class _YdlLogger:
    """Routes yt-dlp messages: full detail to the log file, clean lines to UI."""

    def __init__(self, emit):
        self._emit = emit

    # yt-dlp / plugins may pass extra kwargs (e.g. once=True) — accept them all.
    def debug(self, msg, *args, **kwargs):
        # All of yt-dlp's verbose/debug detail goes to the file, not the window.
        flog(_ANSI.sub("", msg))

    def info(self, msg, *args, **kwargs):
        flog(_ANSI.sub("", msg))

    def warning(self, msg, *args, **kwargs):
        msg = _ANSI.sub("", msg)
        flog("WARNING: " + msg)
        first = msg.strip().splitlines()[0] if msg.strip() else msg
        if not _is_noise(first):
            self._emit(f"  ⚠ {first}")

    def error(self, msg, *args, **kwargs):
        msg = _ANSI.sub("", msg)
        flog("ERROR: " + msg)
        # Verbose mode appends a traceback; show only the headline in the UI.
        first = msg.strip().splitlines()[0] if msg.strip() else msg
        if not _is_noise(first):
            self._emit(f"  ✖ {first}")


def _host_of(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_youtube(url):
    return any(h in _host_of(url) for h in
               ('youtube.com', 'youtu.be', 'youtube-nocookie.com'))


def classify_url(url):
    """Return 'playlist', 'video_in_playlist' or 'single' for a YouTube URL.

    Only YouTube playlist URLs are auto-detected; everything else is 'single'
    so normal links keep adding instantly with no network round-trip."""
    try:
        p = urlparse(url)
        if not _is_youtube(url):
            return 'single'
        q = parse_qs(p.query)
        has_list = bool(q.get('list', [''])[0])
        if p.path.rstrip('/').endswith('/playlist'):
            return 'playlist'
        has_video = bool(q.get('v', [''])[0]) or (
            'youtu.be' in _host_of(url) and p.path.strip('/'))
        if has_list and has_video:
            return 'video_in_playlist'
        if has_list:
            return 'playlist'
        return 'single'
    except Exception:
        return 'single'


def strip_playlist(url):
    """Reduce a watch?v=…&list=… URL to just its single video."""
    try:
        p = urlparse(url)
        q = parse_qs(p.query)
        if q.get('v', [''])[0]:
            return f"https://www.youtube.com/watch?v={q['v'][0]}"
        if 'youtu.be' in _host_of(url):
            return f"https://youtu.be/{p.path.strip('/')}"
    except Exception:
        pass
    return url


def youtube_video_id(url):
    """Best-effort extract a YouTube video id from any of its URL shapes
    (watch?v=, youtu.be/, /shorts/, /embed/, /live/). Returns None if absent —
    lets us build a thumbnail URL with no network call for YouTube links."""
    if not _is_youtube(url):
        return None
    try:
        p = urlparse(url)
        q = parse_qs(p.query)
        if q.get('v', [''])[0]:
            return q['v'][0]
        if 'youtu.be' in _host_of(url):
            return p.path.strip('/').split('/')[0] or None
        parts = [s for s in p.path.split('/') if s]
        if parts and parts[0] in ('shorts', 'embed', 'live', 'v') and len(parts) > 1:
            return parts[1]
    except Exception:
        pass
    return None


# Terminal errors: retrying with another client/format won't help, so stop.
_TERMINAL = (
    "private video", "video unavailable", "this video is unavailable",
    "has been removed", "no longer available", "members-only", "members only",
    "confirm your age", "age-restricted", "sign in to confirm your age",
    "copyright", "not available in your country", "account associated",
    "terminated", "been deleted", "is not available", "live event will begin",
    "premieres in",
)


def _is_terminal(msg):
    low = msg.lower()
    return any(s in low for s in _TERMINAL)


def build_opts(url, fmt, folder, progress_hook, logger, cookies_browser="none",
               compat=False, client_override=None, subtitles=False, sublangs="en"):
    """Build a yt-dlp options dict for a single URL + chosen format.

    compat=True selects YouTube's progressive (≤360p) stream, which bypasses
    SABR / PO-token requirements and downloads when high-res formats fail.
    client_override forces specific YouTube player client(s), e.g. ['tv'].
    subtitles=True writes + embeds subtitles (video formats only); sublangs is
    a space/comma list of language codes (regex ok), or "all".
    """
    opts = dict(COMMON_OPTS)
    opts['outtmpl'] = os.path.join(folder, '%(title)s [%(id)s].%(ext)s')
    opts['progress_hooks'] = [progress_hook]
    opts['logger'] = logger
    if FFMPEG_LOCATION:
        opts['ffmpeg_location'] = FFMPEG_LOCATION  # bundled ffmpeg (packaged build)

    # ---- Format / quality selection -------------------------------------
    if fmt.startswith("MP4"):
        if "1080" in fmt:
            cap = 1080
        elif "720" in fmt:
            cap = 720
        elif "480" in fmt:
            cap = 480
        else:
            cap = None  # "Best"

        if cap:
            opts['format'] = (
                f"bv*[height<={cap}]+ba/b[height<={cap}]/bv*+ba/b"
            )
        else:
            opts['format'] = "bv*+ba/b"

        # Prefer mp4/h264 video + m4a/aac audio so the result plays everywhere.
        opts['format_sort'] = ['res', 'ext:mp4:m4a', 'vcodec:h264', 'acodec:aac']
        opts['merge_output_format'] = 'mp4'

    elif fmt.startswith("MP3"):
        opts['format'] = 'ba/b'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif fmt.startswith("WAV"):
        opts['format'] = 'ba/b'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'wav',
        }]
    elif fmt.startswith("M4A"):
        opts['format'] = 'ba[ext=m4a]/ba/b'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '0',
        }]
    else:
        opts['format'] = 'bv*+ba/b'
        opts['merge_output_format'] = 'mp4'

    # ---- Subtitles (video only — there's nothing to pair them with for
    #      audio-only output) ---------------------------------------------
    is_audio = fmt.startswith(("MP3", "WAV", "M4A"))
    if subtitles and not is_audio:
        raw = (sublangs or "en").strip().lower()
        langs = ['all'] if raw == "all" else (
            [s for s in raw.replace(',', ' ').split() if s] or ['en'])
        opts['writesubtitles'] = True
        opts['writeautomaticsub'] = True       # fall back to auto-generated captions
        opts['subtitleslangs'] = langs
        opts['subtitlesformat'] = 'srt/best'
        # Save a .srt file next to the video (converting YouTube's vtt/json3 to
        # srt). Deliberately NOT embedding: a same-named .srt auto-loads in VLC /
        # MPC / most players so subs "just appear", whereas embedded mov_text is
        # invisible until you manually enable the track — and embedding *deletes*
        # the sidecar, which is what made subtitles look broken before.
        opts.setdefault('postprocessors', []).append(
            {'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'})

    # ---- Platform-specific tuning ---------------------------------------
    if _is_youtube(url):
        # Chunked HTTP dodges YouTube's mid-download throttling / 403 errors.
        opts['http_chunk_size'] = 10 * 1024 * 1024  # 10 MB

        # YouTube's "n challenge" (nsig) now needs an external JavaScript runtime
        # to solve (yt-dlp's "EJS" feature). Point yt-dlp at the bundled Node.js
        # (runtime/node) so we stay self-contained — no system Deno/Node needed.
        # Without a runtime, only image/storyboard formats come back and every
        # download fails with "Requested format is not available". The solver
        # scripts themselves ship in the `yt-dlp-ejs` pip package. yt-dlp enables
        # only Deno by default, so we must name node explicitly + give its path.
        if os.path.exists(NODE_EXE):
            opts['js_runtimes'] = {'node': {'path': NODE_EXE}}

        clients = client_override or YOUTUBE_PLAYER_CLIENTS
        if clients:
            opts['extractor_args'] = {
                'youtube': {'player_client': list(clients)}
            }
        # Cookies unlock high-res formats and stop the "not a bot" check.
        if cookies_browser and cookies_browser.lower() != "none":
            opts['cookiesfrombrowser'] = (cookies_browser.lower(), None, None, None)

        # Compatibility fallback: progressive streams that don't need PO tokens.
        if compat:
            opts.pop('http_chunk_size', None)
            opts.pop('format_sort', None)
            if fmt.startswith(("MP3", "WAV", "M4A")):
                opts['format'] = '140/ba[ext=m4a]/ba/b'
            else:
                opts['format'] = '18/b[ext=mp4][acodec!=none][vcodec!=none]/b'

    return opts


# ===========================================================================
#  Worker threads (keep the UI responsive)
# ===========================================================================
class DownloadWorker(QThread):
    log = Signal(str)
    progress = Signal(int)                  # 0-100 for the current file
    item_started = Signal(int, int, str)    # index, total, url
    item_finished = Signal(str, bool, str, str)  # url, success, error, filepath
    all_finished = Signal(int, int)         # ok count, fail count

    def __init__(self, urls, fmt, folder, cookies_browser="none",
                 subtitles=False, sublangs="en"):
        super().__init__()
        self.urls = urls
        self.fmt = fmt
        self.folder = folder
        self.cookies_browser = cookies_browser
        self.subtitles = subtitles
        self.sublangs = sublangs
        self._cancel = False
        self._announced = False
        self._current_file = None     # best-known output path for this URL

    def cancel(self):
        self._cancel = True

    def _hook(self, d):
        if self._cancel:
            raise _Cancelled()
        status = d.get('status')
        if status == 'downloading':
            # Announce the actual stream once, so it's clear it's working.
            if not self._announced:
                self._announced = True
                info = d.get('info_dict') or {}
                h = info.get('height')
                res = f"{h}p" if h else (info.get('format_note') or info.get('ext') or "stream")
                self.log.emit(f"  ↓ downloading {res}…")
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            done = d.get('downloaded_bytes', 0)
            if total:
                self.progress.emit(max(0, min(100, int(done * 100 / total))))
        elif status == 'finished':
            self.progress.emit(100)
            self._announced = False
            if d.get('filename'):
                self._current_file = d['filename']
            self.log.emit("  ✓ download finished, processing…")

    def _pp_hook(self, d):
        # Postprocessing (merge / audio-extract / embed) rewrites the file, so
        # this is where the *final* path lands — used for "Reveal file".
        if d.get('status') == 'finished':
            info = d.get('info_dict') or {}
            fp = info.get('filepath') or info.get('_filename')
            if fp:
                self._current_file = fp

    def _download(self, url, logger, **kw):
        opts = build_opts(url, self.fmt, self.folder, self._hook, logger,
                          self.cookies_browser, subtitles=self.subtitles,
                          sublangs=self.sublangs, **kw)
        opts['postprocessor_hooks'] = [self._pp_hook]
        flog(f"--- attempt opts: format={opts.get('format')} "
             f"clients={opts.get('extractor_args')} "
             f"chunk={opts.get('http_chunk_size')} subs={opts.get('writesubtitles')}")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    def _strategies(self, url):
        """Ordered (label, build-kwargs) attempts to try for one URL.

        YouTube's SABR/PO-token enforcement makes high-res formats fail with
        403 or empty files, so we escalate: default → tv client → progressive.
        """
        if not _is_youtube(url):
            return [("", {})]
        return [
            ("", {}),                                  # requested quality + cookies
            ("TV client", {"client_override": ["tv"]}),
            ("compatibility ≤360p", {"compat": True}),
        ]

    def run(self):
        logger = _YdlLogger(self.log.emit)
        ok = fail = 0
        total = len(self.urls)
        flog("#" * 70)
        flog(f"SESSION start  format={self.fmt!r}  cookies={self.cookies_browser!r}  "
             f"urls={len(self.urls)}")
        flog(f"yt-dlp={yt_dlp.version.__version__}  python={platform.python_version()}  "
             f"os={platform.platform()}")

        for i, url in enumerate(self.urls, 1):
            if self._cancel:
                break
            self.item_started.emit(i, total, url)
            flog(f"[{i}/{total}] {url}")
            success = False
            last_err = "unknown error"
            self._current_file = None

            for label, kw in self._strategies(url):
                if self._cancel:
                    break
                if label:
                    self.log.emit(f"  ↻ retrying ({label})…")
                flog(f"  >> strategy: {label or 'default'}")
                self.progress.emit(0)
                self._announced = False
                try:
                    self._download(url, logger, **kw)
                    ok += 1
                    success = True
                    self.item_finished.emit(url, True, "", self._current_file or "")
                    break
                except _Cancelled:
                    self.log.emit("Download cancelled by user.")
                    flog("  cancelled by user")
                    self.all_finished.emit(ok, fail)
                    return
                except Exception as e:
                    last_err = str(e)
                    flog(f"  strategy failed: {last_err}")
                    # Stop early only if the video is genuinely unavailable;
                    # otherwise let the remaining strategies have a go.
                    if _is_terminal(last_err):
                        break

            if not success:
                fail += 1
                self.item_finished.emit(url, False, last_err, "")

        flog(f"SESSION end  ok={ok}  fail={fail}")
        self.all_finished.emit(ok, fail)


class UpdateWorker(QThread):
    """Runs `pip install -U "yt-dlp[default]"` without freezing the UI.

    The `[default]` extra is important: it pulls `yt-dlp-ejs`, the JavaScript
    challenge-solver scripts that modern YouTube requires. Updating bare
    `yt-dlp` alone can leave those scripts behind and re-break YouTube.
    """
    log = Signal(str)
    done = Signal(bool)

    def __init__(self):
        super().__init__()
        self._proc = None

    def cancel(self):
        """Terminate the pip subprocess so the thread can exit promptly
        (e.g. the window is closed mid-update)."""
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def run(self):
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out, err = self._proc.communicate()
            tail = (out or "").strip().splitlines()[-3:]
            for line in tail:
                self.log.emit(line)
            if self._proc.returncode == 0:
                self.done.emit(True)
            else:
                self.log.emit((err or "").strip()[-500:])
                self.done.emit(False)
        except Exception as e:
            self.log.emit(f"Update failed: {e}")
            self.done.emit(False)


class LibUpdateWorker(QThread):
    """Keep yt-dlp current in the *packaged* build with no pip / Python on the
    user's PC. Fetches the latest yt-dlp (and the yt-dlp-ejs JS solver scripts)
    straight from PyPI as wheels — which are just zips — and stages the package
    folders under lib_update\\. They're swapped into lib\\ on the next launch by
    _apply_staged_lib_update(). This is the whole reason yt-dlp isn't frozen
    into the .exe (see the bootstrap at the top of this file).

    Used only when IS_FROZEN; a dev run keeps the pip-based UpdateWorker.
    """
    log = Signal(str)
    done = Signal(bool, str)   # (something_changed, message-or-version)

    def __init__(self, current_version):
        super().__init__()
        self._current = current_version or ""
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            info = self._pypi("yt-dlp")
            latest = info["info"]["version"] if info else None
            if not latest:
                self.done.emit(False, "Couldn't reach the update server.")
                return
            if not self._is_newer(latest, self._current):
                self.done.emit(False, "Already up to date (%s)." % self._current)
                return
            self.log.emit("Downloading yt-dlp %s…" % latest)
            ok = self._stage(info, "yt_dlp")
            # The JS solver scripts move much more rarely; refresh best-effort.
            self._stage(self._pypi("yt-dlp-ejs"), "yt_dlp_ejs")
            if ok:
                self.done.emit(True, latest)
            else:
                self.done.emit(False, "Download failed; will retry next launch.")
        except Exception as e:
            flog("LibUpdateWorker error: %r" % e)
            self.done.emit(False, "Update check failed.")

    @staticmethod
    def _is_newer(latest, current):
        """yt-dlp versions are date-based (2026.03.17), but PyPI reports them
        with the zero-padding stripped (2026.3.17). Compare the integer
        components, not the raw strings, so we don't re-download the same
        version on every launch."""
        def parts(v):
            return tuple(int(n) for n in re.findall(r"\d+", v or ""))
        try:
            return parts(latest) > parts(current)
        except Exception:
            return (latest or "") != (current or "")

    def _pypi(self, project):
        """Fetch a project's PyPI JSON metadata (or None on any failure)."""
        import json
        try:
            url = "https://pypi.org/pypi/%s/json" % project
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            flog("PyPI fetch %s failed: %r" % (project, e))
            return None

    def _stage(self, info, pkg_dir):
        """Download the project's wheel and extract its pkg_dir/ tree into the
        staging folder. Returns True only if files were written."""
        import io, zipfile, shutil
        if self._cancel or not info:
            return False
        wheel = next((f["url"] for f in info.get("urls", [])
                      if f.get("packagetype") == "bdist_wheel"
                      and f["filename"].endswith(".whl")), None)
        if not wheel:
            return False
        try:
            with urllib.request.urlopen(wheel, timeout=180) as r:
                blob = r.read()
        except Exception as e:
            flog("wheel download %s failed: %r" % (pkg_dir, e))
            return False
        os.makedirs(LIB_STAGE_DIR, exist_ok=True)
        dest = os.path.join(LIB_STAGE_DIR, pkg_dir)
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        prefix = pkg_dir + "/"
        wrote = False
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for member in z.namelist():
                if member.startswith(prefix) and not member.endswith("/"):
                    z.extract(member, LIB_STAGE_DIR)
                    wrote = True
        return wrote


class ThumbnailWorker(QThread):
    """Fetch a preview image for each link in the main list (background).

    YouTube links are resolved instantly from their video id (no extraction,
    no PO token), so the common case is just an image download. Other sites get
    a best-effort lightweight metadata extraction; if anything fails the link
    simply keeps its placeholder. Emits (url, image bytes) per resolved thumb.
    """
    thumb_ready = Signal(str, object)   # (original link url, image bytes)

    def __init__(self, urls, cookies_browser="none", parent=None):
        super().__init__(parent)
        self.urls = list(urls)
        self.cookies_browser = cookies_browser
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        def handle(url):
            if self._cancel:
                return
            thumb_url = self._resolve(url)
            if not thumb_url or self._cancel:
                return
            data = self._download(thumb_url)
            if data and not self._cancel:
                self.thumb_ready.emit(url, data)

        with ThreadPoolExecutor(max_workers=6) as pool:
            for url in self.urls:
                if self._cancel:
                    break
                pool.submit(handle, url)

    def _resolve(self, url):
        """Return a thumbnail image URL for a link, or None."""
        vid = youtube_video_id(url)
        if vid:
            return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        # Non-YouTube: a quick flat extraction is usually enough for a thumbnail.
        try:
            opts = {
                'quiet': True, 'no_warnings': True, 'skip_download': True,
                'extract_flat': True, 'ignoreerrors': True, 'playlist_items': '1',
                'logger': _YdlLogger(lambda *_: None),
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                return None
            if info.get('entries'):
                info = info['entries'][0] or {}
            thumb = info.get('thumbnail')
            if not thumb:
                thumbs = info.get('thumbnails') or []
                if thumbs:
                    thumb = thumbs[len(thumbs) // 2].get('url') or thumbs[-1].get('url')
            return thumb
        except Exception:
            return None

    def _download(self, thumb_url):
        try:
            req = urllib.request.Request(
                thumb_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                return resp.read()
        except Exception:
            return None


# ===========================================================================
#  Neon-glow theme helpers
# ===========================================================================
# The whole UI is driven by a single accent colour. These are module globals
# so the accent can be swapped at runtime (see DownloaderUI.apply_accent):
# ACCENT_DIM (a darker shade for gradients/hover) and ACCENT_GLOW are derived.
DEFAULT_ACCENT = "#00e676"  # neon emerald
ACCENT = DEFAULT_ACCENT
ACCENT_DIM = "#00c853"      # slightly deeper green for gradients/hover
ACCENT_GLOW = QColor(0, 230, 118)

# A few hand-picked alternates offered next to the colour picker.
ACCENT_PRESETS = ["#00e676", "#00e5ff", "#ff4d6d", "#ffb300", "#b388ff", "#ff6e40"]


def _shade(hexcolor, factor):
    """Return hexcolor with its RGB scaled by factor (darken <1 / lighten >1)."""
    c = QColor(hexcolor)
    return QColor(
        max(0, min(255, int(c.red() * factor))),
        max(0, min(255, int(c.green() * factor))),
        max(0, min(255, int(c.blue() * factor))),
    ).name()


def set_accent(hexcolor):
    """Point the global accent + its derived shades at a new colour."""
    global ACCENT, ACCENT_DIM, ACCENT_GLOW
    ACCENT = QColor(hexcolor).name()
    ACCENT_DIM = _shade(ACCENT, 0.82)
    ACCENT_GLOW = QColor(ACCENT)


def make_app_pixmap(size=256):
    """Paint the app's neon download-arrow logo (no image file needed).

    Used for both the window/taskbar icon and the in-app header logo, so the
    app stays a single self-contained file."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    # Rounded background tile with a subtle green-to-black gradient.
    margin = size * 0.06
    tile = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0.0, QColor("#13211a"))
    grad.setColorAt(1.0, QColor("#0a0a0a"))
    p.setBrush(QBrush(grad))
    p.setPen(QPen(QColor("#1f3b2c"), max(1.0, size * 0.012)))
    p.drawRoundedRect(tile, size * 0.2, size * 0.2)

    cx = size / 2

    def arrow(width, color):
        pen = QPen(color, width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(cx, size * 0.30), QPointF(cx, size * 0.58))  # stem
        ch = size * 0.13
        chevron = QPainterPath()
        chevron.moveTo(cx - ch, size * 0.58 - ch)
        chevron.lineTo(cx, size * 0.58)
        chevron.lineTo(cx + ch, size * 0.58 - ch)
        p.drawPath(chevron)
        tw = size * 0.19
        p.drawLine(QPointF(cx - tw, size * 0.72), QPointF(cx + tw, size * 0.72))  # tray

    glow = QColor(ACCENT_GLOW)
    glow.setAlpha(60)
    arrow(size * 0.135, glow)            # wide translucent pass = fake glow
    arrow(size * 0.058, QColor(ACCENT))  # crisp pass on top
    p.end()
    return pm


def add_glow(widget, color=ACCENT_GLOW, radius=22):
    """Attach a coloured neon glow (a drop shadow with no offset) to a widget.
    Returns the effect so its blurRadius can be animated for a pulse."""
    eff = QGraphicsDropShadowEffect(widget)
    eff.setColor(QColor(color))
    eff.setBlurRadius(radius)
    eff.setOffset(0, 0)
    widget.setGraphicsEffect(eff)
    return eff


# Qt stylesheet for the whole window. Placeholders (@ACCENT@/@DIM@) are
# substituted at apply time by build_qss() so the accent can be re-themed live.
_QSS_TEMPLATE = """
QWidget { background-color: #0a0a0a; color: #e9ece9; font-family: "Segoe UI", "Helvetica Neue", Arial; font-size: 14px; }
QWidget#AppRoot { background-color: #0a0a0a; }
QLabel { background: transparent; }
QLabel#title { font-size: 19px; font-weight: 700; color: #eafff3; letter-spacing: 0.5px; }
QLabel#subtitle { color: #6f7d75; font-size: 11px; }
QLabel#section { color: @ACCENT@; font-size: 11px; font-weight: 700; letter-spacing: 2px; }

QFrame#card { background-color: #121512; border: 1px solid #20271f; border-radius: 14px; }
QFrame#header { background-color: #0e140f; border: 1px solid #1c2a20; border-radius: 14px; }

QLineEdit, QComboBox, QPlainTextEdit, QListWidget {
    background-color: #0c0e0c; border: 1px solid #283028; border-radius: 9px;
    padding: 7px 9px; selection-background-color: @ACCENT@; selection-color: #052012;
}
QPlainTextEdit { font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px; color: #bfe9cf; }
QLineEdit:focus, QComboBox:focus { border: 1px solid @ACCENT@; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #0c0e0c; border: 1px solid #283028; outline: none;
    selection-background-color: @ACCENT@; selection-color: #052012;
}
QListWidget { padding: 4px; }
QListWidget::item { padding: 6px 8px; border-radius: 6px; }
QListWidget::item:selected { background-color: #10261a; color: #c8ffe0; }
QListWidget::indicator { width: 16px; height: 16px; border: 1px solid #3a463c; border-radius: 4px; background: #0c0e0c; }
QListWidget::indicator:checked { background-color: @ACCENT@; border-color: @ACCENT@; }
QListWidget::indicator:hover { border-color: @ACCENT@; }

QCheckBox { spacing: 7px; color: #aeb8b0; font-size: 12px; }
QCheckBox:hover { color: #c8ffe0; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #3a463c; border-radius: 4px; background: #0c0e0c; }
QCheckBox::indicator:checked { background-color: @ACCENT@; border-color: @ACCENT@; }
QCheckBox::indicator:hover { border-color: @ACCENT@; }

QPushButton { background-color: #161a16; border: 1px solid #2c352c; border-radius: 9px; padding: 8px 14px; }
QPushButton:hover { border-color: @ACCENT@; color: #c8ffe0; }
QPushButton:pressed { background-color: #0c1a12; }
QPushButton:disabled { color: #56605a; border-color: #1b201b; background-color: #121412; }
QPushButton#primary { background-color: @ACCENT@; color: #04160c; font-weight: 700; border: 1px solid @ACCENT@; }
QPushButton#primary:hover { background-color: #4dff9e; border-color: #4dff9e; }
QPushButton#primary:disabled { background-color: #16241b; color: #4a6356; border-color: #1d2c22; }
QPushButton#danger:hover { border-color: #ff5252; color: #ffd0d0; }
QPushButton#ghost { background: transparent; }

QProgressBar { border: 1px solid #243024; border-radius: 9px; background-color: #0c0e0c; text-align: center; color: #daffe9; height: 22px; }
QProgressBar::chunk { border-radius: 8px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 @DIM@, stop:1 @ACCENT@); }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #2a352c; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: @DIM@; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { height: 0; }

QSplitter#bodySplit::handle:horizontal { background: transparent; margin: 2px 4px; border-radius: 3px; }
QSplitter#bodySplit::handle:horizontal:hover { background-color: #16241b; }
QSplitter#bodySplit::handle:horizontal:pressed { background-color: @DIM@; }

QToolTip { background-color: #0c0e0c; color: #dafbe7; border: 1px solid @DIM@; padding: 4px; }
"""


def build_qss():
    """The window stylesheet with the current accent substituted in."""
    return _QSS_TEMPLATE.replace("@ACCENT@", ACCENT).replace("@DIM@", ACCENT_DIM)


# ===========================================================================
#  Playlist picker
# ===========================================================================
class PlaylistWorker(QThread):
    """Fetch a playlist's video list (flat, fast — no per-video extraction),
    then download each thumbnail in the background."""
    entries_ready = Signal(list)        # [{index,id,title,duration,url,thumb}]
    thumb_ready = Signal(int, object)   # (row index, image bytes)
    failed = Signal(str)

    def __init__(self, url, cookies_browser="none", parent=None):
        super().__init__(parent)
        self.url = url
        self.cookies_browser = cookies_browser
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            opts = {
                'quiet': True, 'no_warnings': True, 'skip_download': True,
                'extract_flat': 'in_playlist', 'ignoreerrors': True,
                'logger': _YdlLogger(lambda *_: None),
            }
            # Cookies can unlock private/unlisted playlists the user has access
            # to. Flat listing needs no PO token or JS runtime.
            if _is_youtube(self.url) and self.cookies_browser \
                    and self.cookies_browser.lower() != "none":
                opts['cookiesfrombrowser'] = (self.cookies_browser.lower(), None, None, None)

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
            if self._cancel:
                return

            entries = []
            for e in (info.get('entries') or []):
                if not e or not e.get('id'):
                    continue
                vid = e['id']
                entries.append({
                    'index': len(entries),
                    'id': vid,
                    'title': e.get('title') or vid,
                    'duration': e.get('duration'),
                    'url': e.get('webpage_url') or f"https://www.youtube.com/watch?v={vid}",
                    'thumb': self._thumb_url(e, vid),
                })
            if not entries:
                self.failed.emit("No videos found — the playlist may be empty or private.")
                return
            self.entries_ready.emit(entries)
            self._fetch_thumbs(entries)
        except Exception as ex:
            line = str(ex).strip().splitlines()[-1] if str(ex).strip() else ""
            self.failed.emit(line or "Could not read that playlist.")

    def _thumb_url(self, entry, vid):
        thumb = entry.get('thumbnail')
        if not thumb:
            thumbs = entry.get('thumbnails') or []
            if thumbs:
                thumb = (thumbs[len(thumbs) // 2].get('url')
                         or thumbs[-1].get('url'))
        if not thumb and _is_youtube(self.url):
            thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        return thumb

    def _fetch_thumbs(self, entries):
        def fetch(entry):
            if self._cancel or not entry['thumb']:
                return
            try:
                req = urllib.request.Request(
                    entry['thumb'], headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = resp.read()
                if not self._cancel:
                    self.thumb_ready.emit(entry['index'], data)
            except Exception:
                pass  # a missing thumbnail just leaves the placeholder

        with ThreadPoolExecutor(max_workers=8) as pool:
            for entry in entries:
                if self._cancel:
                    break
                pool.submit(fetch, entry)


class PlaylistDialog(QDialog):
    """Modal checklist of a playlist's videos. self.selected_urls holds the
    chosen video URLs after the user clicks Add."""
    THUMB = QSize(120, 68)

    def __init__(self, url, cookies_browser="none", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select videos from playlist")
        self.setModal(True)
        self.resize(700, 560)
        self.selected_urls = []

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)

        self.status = QLabel("Loading playlist…  (reading the video list)")
        v.addWidget(self.status)

        topbar = QHBoxLayout()
        self.sel_all = QPushButton("Select All")
        self.sel_all.setObjectName("ghost")
        self.sel_none = QPushButton("Select None")
        self.sel_none.setObjectName("ghost")
        self.sel_all.clicked.connect(lambda: self._set_all(True))
        self.sel_none.clicked.connect(lambda: self._set_all(False))
        self.sel_all.setEnabled(False)
        self.sel_none.setEnabled(False)
        self.count_lbl = QLabel("")
        topbar.addWidget(self.sel_all)
        topbar.addWidget(self.sel_none)
        topbar.addStretch(1)
        topbar.addWidget(self.count_lbl)
        v.addLayout(topbar)

        self.list = QListWidget()
        self.list.setIconSize(self.THUMB)
        self.list.itemChanged.connect(self._update_count)
        v.addWidget(self.list, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.add_btn = QPushButton("Add selected")
        self.add_btn.setObjectName("primary")
        self.add_btn.setMinimumHeight(34)
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(self.add_btn)
        btns.addWidget(cancel_btn)
        v.addLayout(btns)

        self.worker = PlaylistWorker(url, cookies_browser, self)
        self.worker.entries_ready.connect(self._on_entries)
        self.worker.thumb_ready.connect(self._on_thumb)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    @staticmethod
    def _fmt_dur(secs):
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _placeholder_icon(self):
        pm = QPixmap(self.THUMB)
        pm.fill(QColor("#15191a"))
        return QIcon(pm)

    def _on_entries(self, entries):
        self.status.setText(
            f"{len(entries)} videos — tick the ones you want, then “Add selected”.")
        self.list.blockSignals(True)
        for e in entries:
            dur = self._fmt_dur(e['duration']) if e['duration'] else "—"
            item = QListWidgetItem(f"{e['index'] + 1}.  {e['title']}   ({dur})")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)          # default: none selected
            item.setData(Qt.UserRole, e['url'])
            item.setIcon(self._placeholder_icon())
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.sel_all.setEnabled(True)
        self.sel_none.setEnabled(True)
        self._update_count()

    def _on_thumb(self, index, data):
        if 0 <= index < self.list.count():
            pm = QPixmap()
            if pm.loadFromData(bytes(data)):
                pm = pm.scaled(self.THUMB, Qt.KeepAspectRatio,
                               Qt.SmoothTransformation)
                self.list.item(index).setIcon(QIcon(pm))

    def _on_failed(self, msg):
        self.status.setText("⚠ " + msg)

    def _set_all(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)
        self.list.blockSignals(False)
        self._update_count()

    def _update_count(self, *_):
        n = sum(1 for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked)
        self.add_btn.setEnabled(n > 0)
        self.add_btn.setText(f"Add {n} selected" if n else "Add selected")
        self.count_lbl.setText(f"{n} selected")

    def _accept(self):
        self.selected_urls = [
            self.list.item(i).data(Qt.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.Checked
        ]
        self.accept()

    def closeEvent(self, event):
        self.worker.cancel()
        self.worker.wait(3000)
        super().closeEvent(event)

    def reject(self):
        self.worker.cancel()
        super().reject()


# ===========================================================================
#  Main window
# ===========================================================================
class DownloaderUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("AppRoot")
        self.setWindowTitle("Video / Audio Downloader")
        self.settings = QSettings("VidAudDownloader", "DownloaderUI")
        self.worker = None
        self.updater = None
        self._lib_updater = None
        self._thumb_worker = None
        self._tray = None
        self._cancelled = False        # set when the user cancels a batch
        self._active_flashes = set()   # live button-glow pulses (kept from GC)

        # Restore the saved accent colour before any theming/icon is built.
        saved_accent = self.settings.value("accent", "")
        if saved_accent and QColor(saved_accent).isValid() \
                and saved_accent.lower() != DEFAULT_ACCENT.lower():
            set_accent(saved_accent)
        self.setWindowIcon(QIcon(make_app_pixmap(256)))
        # Per-item state glyph + colour as an instance dict so the "downloading"
        # accent tracks live theme changes (shadows the class default).
        self._rebuild_item_states()

        # Open at a comfortable size relative to the screen, centred. The window
        # is freely resizable (no forced aspect ratio); the generous minimum
        # size below is what actually keeps widgets from ever overlapping —
        # Qt won't let the user shrink the window past the layout's needs.
        screen = QApplication.primaryScreen().availableGeometry()
        win_w = min(max(screen.width() // 3, 900), screen.width() - 80)
        win_h = min(max(int(screen.height() * 0.66), 640), screen.height() - 80)
        self.setMinimumSize(720, 560)
        self.resize(win_w, win_h)
        self.move(max(screen.left(), screen.center().x() - win_w // 2),
                  max(screen.top(), screen.center().y() - win_h // 2))
        self.setAcceptDrops(True)
        self._win_w = win_w

        self.setStyleSheet(build_qss())

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        # ============================ Header strip ============================
        header = QFrame()
        header.setObjectName("header")
        hb = QHBoxLayout(header)
        hb.setContentsMargins(16, 12, 16, 12)
        hb.setSpacing(14)
        self._logo = QLabel()
        self._logo.setPixmap(make_app_pixmap(46))
        self._logo_glow = add_glow(self._logo, radius=18)
        logo = self._logo
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title = QLabel("Video / Audio Downloader")
        title.setObjectName("title")
        self._title_glow = add_glow(title, radius=16)
        subtitle = QLabel("YouTube · TikTok · Instagram · X · and most other sites")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        self.state_pill = QLabel()
        self._set_pill(self.state_pill, "Ready", "idle")
        hb.addWidget(logo)
        hb.addLayout(title_box)
        hb.addStretch(1)
        hb.addWidget(self.state_pill)
        root.addWidget(header)

        # ===================== Body: links | options + log ====================
        # A draggable splitter lets the user give either side more room (e.g.
        # a taller log or a wider link list). Neither pane can be collapsed to
        # nothing, and each has a minimum width so the contents stay readable.
        body = QSplitter(Qt.Horizontal)
        body.setObjectName("bodySplit")
        body.setChildrenCollapsible(False)
        body.setHandleWidth(12)

        # ---- LEFT column: links --------------------------------------------
        left = QFrame()
        left.setObjectName("card")
        left.setMinimumWidth(300)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(14, 12, 14, 14)
        lv.setSpacing(9)

        links_header = QHBoxLayout()
        links_header.addWidget(self._section("LINKS"))
        links_header.addStretch(1)
        self.thumbs_check = QCheckBox("Thumbnails")
        self.thumbs_check.setToolTip(
            "Show a preview image beside each link.\n"
            "Its border turns green when the download succeeds, red if it fails."
        )
        self.thumbs_check.toggled.connect(self._on_thumbs_toggled)
        links_header.addWidget(self.thumbs_check)
        lv.addLayout(links_header)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText(
            "Paste a link and press Enter — or drag links onto the window"
        )
        self.link_input.returnPressed.connect(self.add_link)
        add_link_btn = QPushButton("Add")
        add_link_btn.clicked.connect(self.add_link)
        input_row.addWidget(self.link_input, 1)
        input_row.addWidget(add_link_btn)
        lv.addLayout(input_row)

        self.link_list = QListWidget()
        self.link_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.link_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.link_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.link_list.customContextMenuRequested.connect(self._link_menu)
        self.link_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        lv.addWidget(self.link_list, 1)

        link_btn_row = QHBoxLayout()
        remove_btn = QPushButton("Remove Selected")
        remove_btn.setObjectName("ghost")
        remove_btn.clicked.connect(self.remove_selected)
        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("ghost")
        clear_btn.clicked.connect(self.clear_links)
        self.retry_btn = QPushButton("Retry Failed")
        self.retry_btn.setObjectName("ghost")
        self.retry_btn.setToolTip("Re-download only the links that failed last run.")
        self.retry_btn.clicked.connect(self.retry_failed)
        self.retry_btn.setEnabled(False)
        link_btn_row.addWidget(remove_btn)
        link_btn_row.addWidget(clear_btn)
        link_btn_row.addWidget(self.retry_btn)
        link_btn_row.addStretch(1)
        lv.addLayout(link_btn_row)

        # ---- RIGHT column: options card + log card -------------------------
        right_panel = QWidget()
        right_panel.setMinimumWidth(340)
        right = QVBoxLayout(right_panel)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(12)

        opts = QFrame()
        opts.setObjectName("card")
        ov = QVBoxLayout(opts)
        ov.setContentsMargins(14, 12, 14, 14)
        ov.setSpacing(9)
        ov.addWidget(self._section("OPTIONS"))

        folder_layout = QHBoxLayout()
        folder_layout.setSpacing(8)
        self.folder_label = QLabel("No folder selected")
        self.folder_label.setStyleSheet("color:#9aa39c;")
        self.folder_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        folder_btn = QPushButton("Choose")
        folder_btn.clicked.connect(self.choose_folder)
        self._folder_glow = add_glow(folder_btn, radius=0)   # lit while opening
        open_folder_btn = QPushButton("Open")
        open_folder_btn.setObjectName("ghost")
        open_folder_btn.clicked.connect(self.open_folder)
        self._open_glow = add_glow(open_folder_btn, radius=0)
        folder_layout.addWidget(QLabel("Folder:"))
        folder_layout.addWidget(self.folder_label, 1)
        folder_layout.addWidget(folder_btn)
        folder_layout.addWidget(open_folder_btn)
        ov.addLayout(folder_layout)

        fc_layout = QHBoxLayout()
        fc_layout.setSpacing(8)
        fc_layout.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems([
            "MP4 — Best quality",
            "MP4 — 1080p",
            "MP4 — 720p",
            "MP4 — 480p",
            "MP3 — Audio",
            "WAV — Audio",
            "M4A — Audio (original)",
        ])
        fc_layout.addWidget(self.format_combo, 1)
        fc_layout.addSpacing(6)
        fc_layout.addWidget(QLabel("Cookies:"))
        self.cookie_combo = QComboBox()
        # (display label, stored value, per-item hover tooltip). Display text and
        # value are kept separate so the label can be decorated without breaking
        # the browser name yt-dlp receives (read via currentData()).
        cookie_choices = [
            ("Firefox  ✓ recommended", "firefox",
             "Recommended. On Windows, Firefox is the only browser whose cookies\n"
             "reliably work. Log into YouTube in Firefox, then pick this."),
            ("Chrome", "chrome",
             "Usually fails on Windows — Chrome's app-bound cookie encryption\n"
             "blocks yt-dlp from reading the cookies. Use Firefox instead."),
            ("Brave", "brave",
             "Usually fails on Windows — Brave (Chromium) uses the same app-bound\n"
             "cookie encryption as Chrome. Use Firefox instead."),
            ("None (no cookies)", "none",
             "Use no cookies. YouTube may be capped at ~360p or hit 'not a bot'\n"
             "checks. Cookies are only ever used for YouTube."),
        ]
        for label, value, tip in cookie_choices:
            self.cookie_combo.addItem(label, value)
            self.cookie_combo.setItemData(
                self.cookie_combo.count() - 1, tip, Qt.ToolTipRole)
        self.cookie_combo.setToolTip(
            "Borrow cookies from a browser you're logged into YouTube with —\n"
            "needed for 1080p+ and to avoid 'not a bot' errors. Firefox is the only\n"
            "reliable choice on Windows; hover an option for details. (YouTube only.)"
        )
        fc_layout.addWidget(self.cookie_combo, 1)
        ov.addLayout(fc_layout)

        # Extra toggles: subtitles (+ language) and a finish notification.
        extras_row = QHBoxLayout()
        extras_row.setSpacing(8)
        self.subs_check = QCheckBox("Subtitles")
        self.subs_check.setToolTip(
            "Save subtitles / auto-captions as a .srt file next to the video\n"
            "(video formats only). Same-named .srt files auto-load in VLC and\n"
            "most players, so the subs just appear when you play it.")
        self.subs_lang = QComboBox()
        for label, code in [
            ("English", "en"), ("Spanish", "es"), ("French", "fr"),
            ("German", "de"), ("Portuguese", "pt"), ("Italian", "it"),
            ("Russian", "ru"), ("Japanese", "ja"), ("Korean", "ko"),
            ("Chinese", "zh"), ("Arabic", "ar"), ("Hindi", "hi"),
            ("All available", "all"),
        ]:
            self.subs_lang.addItem(label, code)
        self.subs_lang.setToolTip(
            "Which subtitle language to fetch + embed (falls back to\n"
            "auto-generated captions if there's no human-made track).")
        self.subs_lang.setEnabled(False)
        self.subs_check.toggled.connect(self.subs_lang.setEnabled)
        self.notify_check = QCheckBox("Notify when done")
        self.notify_check.setToolTip(
            "Pop a system notification and a soft chime when a batch finishes,\n"
            "so you can switch away while it works.")
        extras_row.addWidget(self.subs_check)
        extras_row.addWidget(self.subs_lang)
        extras_row.addStretch(1)
        extras_row.addWidget(self.notify_check)
        ov.addLayout(extras_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.download_btn = QPushButton("↓  Start Download")
        self.download_btn.setObjectName("primary")
        self.download_btn.setMinimumHeight(38)
        self.download_btn.clicked.connect(self.start_download)
        self._dl_glow = add_glow(self.download_btn, radius=20)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setMinimumHeight(38)
        self.cancel_btn.clicked.connect(self.cancel_download)
        self.cancel_btn.setEnabled(False)
        action_row.addWidget(self.download_btn, 3)
        action_row.addWidget(self.cancel_btn, 1)
        ov.addLayout(action_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self._progress_glow = add_glow(self.progress_bar, radius=2)
        ov.addWidget(self.progress_bar)
        right.addWidget(opts)

        log_card = QFrame()
        log_card.setObjectName("card")
        lgv = QVBoxLayout(log_card)
        lgv.setContentsMargins(14, 12, 14, 14)
        lgv.setSpacing(9)
        lgv.addWidget(self._section("LOG"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        lgv.addWidget(self.log_view, 1)
        right.addWidget(log_card, 1)

        body.addWidget(left)
        body.addWidget(right_panel)
        body.setStretchFactor(0, 4)   # both panes grow when the window does,
        body.setStretchFactor(1, 5)   # with the right side gaining a bit more
        body.setSizes([int(self._win_w * 0.44), int(self._win_w * 0.56)])
        root.addWidget(body, 1)

        # ============================== Footer ===============================
        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.pot_label = QLabel("PO-token provider: checking…")
        self.pot_label.setToolTip(
            "Local helper that lets YouTube release 1080p+ streams and avoids "
            "'empty file' errors. Auto-started from the runtime folder.")
        self._set_pill(self.pot_label, "● PO-token: checking…", "idle")
        self.version_label = QLabel(f"yt-dlp {yt_dlp.version.__version__}")
        self.version_label.setStyleSheet("color:#6f7d75;")
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("ghost")
        self.theme_btn.setFixedSize(26, 26)
        self.theme_btn.setToolTip("Theme colour — click to pick the accent.")
        self.theme_btn.clicked.connect(self._pick_accent)
        self._refresh_theme_swatch()
        log_btn = QPushButton("Open Log")
        log_btn.setObjectName("ghost")
        log_btn.setToolTip(LOG_FILE)
        log_btn.clicked.connect(self.open_log)
        self._log_glow = add_glow(log_btn, radius=0)
        self.update_btn = QPushButton("Update yt-dlp")
        self.update_btn.setObjectName("ghost")
        self.update_btn.clicked.connect(self.update_ytdlp)
        footer.addWidget(self.pot_label)
        footer.addStretch(1)
        footer.addWidget(self.version_label)
        footer.addWidget(self.theme_btn)
        footer.addWidget(log_btn)
        footer.addWidget(self.update_btn)
        root.addLayout(footer)

        # Progress animations: smooth value changes + a breathing neon glow.
        self._value_anim = QPropertyAnimation(self.progress_bar, b"value", self)
        self._value_anim.setDuration(180)
        self._value_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._pulse_anim = QPropertyAnimation(self._progress_glow, b"blurRadius", self)
        self._pulse_anim.setDuration(1100)
        self._pulse_anim.setLoopCount(-1)
        self._pulse_anim.setKeyValueAt(0.0, 6)
        self._pulse_anim.setKeyValueAt(0.5, 26)
        self._pulse_anim.setKeyValueAt(1.0, 6)

        # Restore last-used folder / format.
        saved_folder = self.settings.value("folder", "")
        if saved_folder and os.path.isdir(saved_folder):
            self.output_folder = saved_folder
            self.folder_label.setText(saved_folder)
        else:
            self.output_folder = None
        saved_fmt = self.settings.value("format", "")
        if saved_fmt:
            idx = self.format_combo.findText(saved_fmt)
            if idx >= 0:
                self.format_combo.setCurrentIndex(idx)
        saved_cookie = self.settings.value("cookies_browser", DEFAULT_COOKIES_BROWSER)
        idx = self.cookie_combo.findData(saved_cookie)
        self.cookie_combo.setCurrentIndex(idx if idx >= 0 else 0)
        # Thumbnails are off by default; restoring a True value fires the toggle
        # handler, which sets the icon size (the list is still empty here).
        self.thumbs_check.setChecked(
            self.settings.value("show_thumbs", False, type=bool))
        self.subs_check.setChecked(self.settings.value("subtitles", False, type=bool))
        si = self.subs_lang.findData(self.settings.value("sublangs", "en"))
        self.subs_lang.setCurrentIndex(si if si >= 0 else 0)
        self.notify_check.setChecked(
            self.settings.value("notify", True, type=bool))
        # Persist these on change (connected after restore so it doesn't re-fire).
        self.subs_check.toggled.connect(
            lambda v: self.settings.setValue("subtitles", v))
        self.subs_lang.currentIndexChanged.connect(
            lambda *_: self.settings.setValue("sublangs", self.subs_lang.currentData()))
        self.notify_check.toggled.connect(
            lambda v: self.settings.setValue("notify", v))

        self._check_ffmpeg()

        # Launch the bundled PO-token provider, then poll its status.
        self._pot_proc = start_pot_server()
        self._update_pot_status()
        QTimer.singleShot(2500, self._update_pot_status)
        QTimer.singleShot(6000, self._update_pot_status)

        # Packaged build: quietly check PyPI for a newer yt-dlp a few seconds
        # after startup and stage it for next launch, so it keeps working for
        # non-technical users without any pip/console step on their part.
        if IS_FROZEN:
            QTimer.singleShot(4000, lambda: self._start_lib_update(manual=False))

    # ---- small UI helpers ----
    def _section(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("section")
        return lbl

    def _set_pill(self, label, text, kind):
        """Style a QLabel as a coloured status 'pill'."""
        styles = {
            # kind:   (text,      background, border)
            "idle": ("#9aa39c", "#161b16", "#2a332a"),
            "busy": ("#04160c", ACCENT,     ACCENT),
            "ok":   ("#aef5c8", "#0f2a1b",  ACCENT_DIM),
            "warn": ("#f0c27a", "#241a0c",  "#7a5a1e"),
            "err":  ("#ff9c9c", "#241010",  "#7a2020"),
        }
        fg, bg, border = styles.get(kind, styles["idle"])
        label.setText(text)
        label.setStyleSheet(
            f"background-color:{bg}; color:{fg}; border:1px solid {border};"
            f"border-radius:11px; padding:3px 12px; font-size:12px; font-weight:600;")

    def _set_progress(self, value):
        """Animate the progress bar smoothly toward the new value."""
        self._value_anim.stop()
        self._value_anim.setStartValue(self.progress_bar.value())
        self._value_anim.setEndValue(int(value))
        self._value_anim.start()

    def _update_pot_status(self):
        if pot_server_running():
            self._set_pill(self.pot_label, "● PO-token: running", "ok")
        elif not (os.path.exists(NODE_EXE) and os.path.exists(POT_SERVER_JS)):
            self._set_pill(self.pot_label, "● PO-token: not installed", "warn")
        else:
            self._set_pill(self.pot_label, "● PO-token: starting…", "idle")

    # ---- theme / accent colour ----
    def _rebuild_item_states(self):
        """Per-item glyph + colour, with 'downloading' tracking the live accent."""
        self._ITEM_STATES = {
            "pending":     ("•", "#8b948c"),
            "downloading": ("↓", ACCENT),
            "done":        ("✓", "#5df2a0"),
            "failed":      ("✗", "#ff6b6b"),
        }

    def _refresh_theme_swatch(self):
        self.theme_btn.setStyleSheet(
            f"background-color:{ACCENT}; border:1px solid {ACCENT_DIM};"
            f"border-radius:7px;")

    def _pick_accent(self):
        """Popup of preset swatches + a custom picker; applies live."""
        menu = QMenu(self)
        for hexc in ACCENT_PRESETS:
            act = QAction(hexc, menu)
            pm = QPixmap(14, 14)
            pm.fill(QColor(hexc))
            act.setIcon(QIcon(pm))
            act.triggered.connect(lambda _=False, c=hexc: self.apply_accent(c))
            menu.addAction(act)
        menu.addSeparator()
        custom = QAction("Custom…", menu)
        custom.triggered.connect(self._pick_accent_custom)
        menu.addAction(custom)
        menu.exec(self.theme_btn.mapToGlobal(self.theme_btn.rect().bottomLeft()))

    def _pick_accent_custom(self):
        col = QColorDialog.getColor(QColor(ACCENT), self, "Pick an accent colour")
        if col.isValid():
            self.apply_accent(col.name())

    def apply_accent(self, hexcolor):
        """Swap the whole UI's accent colour at runtime and remember it."""
        set_accent(hexcolor)
        self.settings.setValue("accent", ACCENT)
        self.setStyleSheet(build_qss())
        self._rebuild_item_states()
        # Re-tint every neon glow we keep a handle to.
        for eff in (self._progress_glow, self._folder_glow, self._open_glow,
                    self._log_glow, self._dl_glow, self._logo_glow,
                    self._title_glow):
            eff.setColor(QColor(ACCENT_GLOW))
        # Regenerate the accent-tinted logo + window icon.
        self._logo.setPixmap(make_app_pixmap(46))
        self.setWindowIcon(QIcon(make_app_pixmap(256)))
        if self._tray is not None:
            self._tray.setIcon(QIcon(make_app_pixmap(256)))
        self._refresh_theme_swatch()
        # Refresh anything coloured by hand (pills + already-rendered rows).
        self._update_pot_status()
        self._set_pill(self.state_pill, "Working…" if self._is_busy() else "Ready",
                       "busy" if self._is_busy() else "idle")
        for i in range(self.link_list.count()):
            it = self.link_list.item(i)
            self._set_item_state(it, it.data(self._ROLE_STATE) or "pending",
                                 tip=it.toolTip())

    def _is_busy(self):
        return bool(self.worker and self.worker.isRunning())

    # ---- logging helper ----
    def log(self, msg):
        self.log_view.appendPlainText(msg)
        flog("UI: " + msg)

    def _check_ffmpeg(self):
        from shutil import which
        if FFMPEG_LOCATION is None and which("ffmpeg") is None:
            self.log(
                "⚠ ffmpeg was not found on PATH. Merging video+audio and "
                "audio conversion (MP3/WAV) will fail. Install it from "
                "https://www.gyan.dev/ffmpeg/builds/ and add it to PATH."
            )

    # === "Working…" feedback for clicks that open a slow OS window ==========
    def _flash_button(self, effect, ms=1700):
        """Breathe a neon glow on a button + show a busy cursor for a moment,
        so a click that opens a slow OS window (Explorer, the default editor)
        visibly registers instead of looking like nothing happened. Used for
        the *non-blocking* opens, where the event loop is free to animate."""
        anim = QPropertyAnimation(effect, b"blurRadius", self)
        anim.setDuration(850)
        anim.setLoopCount(-1)
        anim.setKeyValueAt(0.0, 2)
        anim.setKeyValueAt(0.5, 22)
        anim.setKeyValueAt(1.0, 2)
        self._active_flashes.add(anim)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        anim.start()

        def stop():
            anim.stop()
            effect.setBlurRadius(0)
            self._active_flashes.discard(anim)
            QApplication.restoreOverrideCursor()   # balances the push above
        QTimer.singleShot(ms, stop)

    # === Folder selection ===
    def choose_folder(self):
        # Open the picker straight to the last-used folder. Handing the native
        # dialog a concrete starting directory avoids it enumerating "This PC"
        # (every drive + network location + shell extension) on launch, which
        # is what makes it take several seconds to appear.
        #
        # The native dialog is *modal* and blocks the event loop while Windows
        # builds it, so no in-app animation can play during that gap — but the
        # busy cursor (animated by the OS, not us) does, and we light the
        # button's glow first so the click clearly registers.
        start = self.output_folder or os.path.expanduser("~")
        QApplication.setOverrideCursor(Qt.BusyCursor)
        self._folder_glow.setBlurRadius(20)
        QApplication.processEvents()   # paint the busy state before we block
        try:
            folder = QFileDialog.getExistingDirectory(
                self, "Select Download Folder", start,
                QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks)
        finally:
            self._folder_glow.setBlurRadius(0)
            QApplication.restoreOverrideCursor()
        if folder:
            self.output_folder = folder
            self.folder_label.setText(folder)
            self.settings.setValue("folder", folder)

    def open_folder(self):
        if not self.output_folder:
            QMessageBox.warning(self, "Error", "No folder selected.")
            return
        self._flash_button(self._open_glow)   # Explorer launch is non-blocking
        self._open_path(self.output_folder)

    def open_log(self):
        if not os.path.exists(LOG_FILE):
            QMessageBox.information(self, "Log", "No log file yet — run a download first.")
            return
        self._flash_button(self._log_glow)
        self._open_path(LOG_FILE)

    def _open_path(self, path):
        try:
            if sys.platform == "win32":
                # Folders: launch Explorer directly. os.startfile() routes
                # through ShellExecute, whose DDE handshake (to reuse an open
                # Explorer window) can stall the UI for seconds; explorer.exe
                # opens immediately and returns without blocking. Files (the
                # log) keep os.startfile so they open in their default app.
                if os.path.isdir(path):
                    subprocess.Popen(["explorer", os.path.normpath(path)])
                else:
                    os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not open:\n{e}")

    # === Link management ===
    _ITEM_STATES = {
        # state:       (glyph, colour)
        "pending":     ("•", "#8b948c"),
        "downloading": ("↓", ACCENT),
        "done":        ("✓", "#5df2a0"),
        "failed":      ("✗", "#ff6b6b"),
    }
    THUMB = QSize(96, 54)            # 16:9 preview size in the links list
    _ROLE_STATE = Qt.UserRole + 1    # per-item download state (str)
    _ROLE_THUMB = Qt.UserRole + 2    # per-item cached thumbnail (QPixmap)
    _ROLE_FILE = Qt.UserRole + 3     # per-item finished output filepath (str)

    def _item_url(self, item):
        return item.data(Qt.UserRole) if item else None

    def _find_item(self, url):
        for i in range(self.link_list.count()):
            it = self.link_list.item(i)
            if self._item_url(it) == url:
                return it
        return None

    def _set_item_state(self, item, state, tip=None):
        glyph, color = self._ITEM_STATES.get(state, self._ITEM_STATES["pending"])
        url = self._item_url(item)
        item.setText(f"{glyph}  {url}")
        item.setForeground(QColor(color))
        item.setData(self._ROLE_STATE, state)
        item.setToolTip(tip if tip is not None else url)
        # When thumbnails are on, recolour the preview's border to match state.
        if self._thumbs_enabled():
            item.setIcon(self._compose_thumb(item.data(self._ROLE_THUMB), state))

    def _add_link_item(self, url):
        item = QListWidgetItem()
        item.setData(Qt.UserRole, url)
        self.link_list.addItem(item)
        self._set_item_state(item, "pending")

    def _add_urls(self, tokens):
        """Add any new http(s) tokens to the list; returns how many were added."""
        existing = {self._item_url(self.link_list.item(i))
                    for i in range(self.link_list.count())}
        added = 0
        for token in tokens:
            token = token.strip()
            if token.startswith("http") and token not in existing:
                self._add_link_item(token)
                existing.add(token)
                added += 1
        if added:
            self._refresh_thumbnails()
        return added

    # === Thumbnails ===
    def _thumbs_enabled(self):
        return self.thumbs_check.isChecked()

    def _compose_thumb(self, pm, state):
        """Build a QIcon: the thumbnail (or a placeholder) inside a rounded,
        state-coloured border (grey pending, accent downloading, green/red)."""
        w, h = self.THUMB.width(), self.THUMB.height()
        canvas = QPixmap(self.THUMB)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.Antialiasing)
        radius = 7.0
        inner = QRectF(1.5, 1.5, w - 3, h - 3)
        path = QPainterPath()
        path.addRoundedRect(inner, radius, radius)

        p.save()
        p.setClipPath(path)
        if pm is not None and not pm.isNull():
            scaled = pm.scaled(self.THUMB, Qt.KeepAspectRatioByExpanding,
                               Qt.SmoothTransformation)
            p.drawPixmap((w - scaled.width()) // 2,
                         (h - scaled.height()) // 2, scaled)
        else:
            p.fillRect(inner, QColor("#15191a"))
        p.restore()

        _, color = self._ITEM_STATES.get(state, self._ITEM_STATES["pending"])
        pen = QPen(QColor(color), 2.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(inner, radius, radius)
        p.end()
        return QIcon(canvas)

    def _on_thumbs_toggled(self, checked):
        self.settings.setValue("show_thumbs", checked)
        if checked:
            self.link_list.setIconSize(self.THUMB)
            self.link_list.setSpacing(2)
            for i in range(self.link_list.count()):
                it = self.link_list.item(i)
                state = it.data(self._ROLE_STATE) or "pending"
                it.setIcon(self._compose_thumb(it.data(self._ROLE_THUMB), state))
            self._refresh_thumbnails()
        else:
            for i in range(self.link_list.count()):
                self.link_list.item(i).setIcon(QIcon())
            self.link_list.setIconSize(QSize(0, 0))
            self.link_list.setSpacing(0)

    def _refresh_thumbnails(self):
        """Fetch previews for any links that don't have one cached yet."""
        if not self._thumbs_enabled():
            return
        missing = [self._item_url(self.link_list.item(i))
                   for i in range(self.link_list.count())
                   if self.link_list.item(i).data(self._ROLE_THUMB) is None]
        if not missing:
            return
        # A fresh worker covers everything still missing; drop any earlier run.
        if self._thumb_worker and self._thumb_worker.isRunning():
            self._thumb_worker.cancel()
        self._thumb_worker = ThumbnailWorker(
            missing, self.cookie_combo.currentData(), self)
        self._thumb_worker.thumb_ready.connect(self._on_thumb_ready)
        self._thumb_worker.start()

    def _on_thumb_ready(self, url, data):
        item = self._find_item(url)
        if item is None:
            return
        pm = QPixmap()
        if not pm.loadFromData(bytes(data)):
            return
        item.setData(self._ROLE_THUMB, pm)
        if self._thumbs_enabled():
            state = item.data(self._ROLE_STATE) or "pending"
            item.setIcon(self._compose_thumb(pm, state))

    def _intake(self, tokens):
        """Classify tokens, route playlist links to the picker, add the rest.
        Returns how many links were added to the list."""
        normal = []
        added = 0
        for token in tokens:
            token = token.strip()
            if not token.startswith("http"):
                continue
            kind = classify_url(token)
            if kind == "playlist":
                added += self._open_playlist_picker(token)
            elif kind == "video_in_playlist":
                choice = self._ask_video_or_playlist()
                if choice == "video":
                    normal.append(strip_playlist(token))
                elif choice == "playlist":
                    added += self._open_playlist_picker(token)
                # "cancel" → skip this token
            else:
                normal.append(token)
        if normal:
            added += self._add_urls(normal)
        return added

    def _open_playlist_picker(self, url):
        """Show the playlist checklist; add the chosen videos. Returns count."""
        dlg = PlaylistDialog(url, self.cookie_combo.currentData(), self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_urls:
            n = self._add_urls(dlg.selected_urls)
            if n:
                self.log(f"Added {n} video(s) from the playlist.")
            return n
        return 0

    def _ask_video_or_playlist(self):
        box = QMessageBox(self)
        box.setWindowTitle("Video or playlist?")
        box.setIcon(QMessageBox.Question)
        box.setText("This link is a video that's also part of a playlist.\n"
                    "What would you like to add?")
        video_btn = box.addButton("This video", QMessageBox.AcceptRole)
        playlist_btn = box.addButton("Whole playlist", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is video_btn:
            return "video"
        if clicked is playlist_btn:
            return "playlist"
        return "cancel"

    def add_link(self):
        # Accept several links at once (whitespace / newline separated).
        raw = self.link_input.text().strip()
        if not raw:
            return
        tokens = raw.split()
        self._intake(tokens)
        if any(t.strip().startswith("http") for t in tokens):
            self.link_input.clear()
        else:
            QMessageBox.warning(
                self, "Invalid URL",
                "Please enter at least one valid http(s) link."
            )

    def remove_selected(self):
        for item in self.link_list.selectedItems():
            self.link_list.takeItem(self.link_list.row(item))

    def clear_links(self):
        self.link_list.clear()
        self.retry_btn.setEnabled(False)

    # === Right-click context menu / double-click ===
    def _link_menu(self, pos):
        item = self.link_list.itemAt(pos)
        if item is None:
            return
        url = self._item_url(item)
        state = item.data(self._ROLE_STATE) or "pending"
        filepath = item.data(self._ROLE_FILE)
        menu = QMenu(self)
        menu.addAction("Copy link", lambda: QApplication.clipboard().setText(url))
        menu.addAction("Open source page",
                       lambda: QDesktopServices.openUrl(QUrl(url)))
        if filepath and os.path.exists(filepath):
            menu.addAction("Reveal file in folder",
                           lambda: self._reveal_file(filepath))
        menu.addSeparator()
        retry = menu.addAction("Retry this link",
                               lambda: self._run_download([url]))
        retry.setEnabled(not self._is_busy())
        menu.addAction("Remove",
                       lambda: self.link_list.takeItem(self.link_list.row(item)))
        menu.exec(self.link_list.mapToGlobal(pos))

    def _on_item_double_clicked(self, item):
        # Play the finished file if we have it, otherwise open the source page.
        filepath = item.data(self._ROLE_FILE)
        if filepath and os.path.exists(filepath):
            self._open_path(filepath)
        else:
            QDesktopServices.openUrl(QUrl(self._item_url(item)))

    def _reveal_file(self, path):
        """Open the folder with the file selected (Windows), else open folder."""
        try:
            if sys.platform == "win32":
                # Must be one command line: explorer /select,"C:\full\path".
                # As a separate Popen arg, explorer ignores it and just opens
                # the default folder, so build the string ourselves.
                subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not reveal file:\n{e}")

    # === Drag-and-drop (the input box, Enter and Add still work too) ===
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            event.acceptProposedAction()

    def dropEvent(self, event):
        md = event.mimeData()
        tokens = []
        if md.hasUrls():
            tokens += [u.toString() for u in md.urls()]
        if md.hasText():
            tokens += md.text().split()
        added = self._intake(tokens)
        if added:
            self.log(f"Added {added} link(s) via drag-and-drop.")
        event.acceptProposedAction()

    # === Download logic ===
    def start_download(self):
        if self.worker and self.worker.isRunning():
            return
        if self.link_list.count() == 0:
            QMessageBox.warning(self, "Error", "Please add at least one link.")
            return
        urls = [self._item_url(self.link_list.item(i))
                for i in range(self.link_list.count())]
        self._run_download(urls)

    def retry_failed(self):
        """Re-download just the links that failed last run."""
        failed = [self._item_url(self.link_list.item(i))
                  for i in range(self.link_list.count())
                  if self.link_list.item(i).data(self._ROLE_STATE) == "failed"]
        if not failed:
            QMessageBox.information(self, "Retry", "No failed links to retry.")
            return
        self._run_download(failed)

    def _run_download(self, urls):
        """Shared entry point: validate, configure, and launch a DownloadWorker
        for the given list of URLs (used by Start, Retry Failed, and the
        right-click 'Retry this link')."""
        if self.worker and self.worker.isRunning():
            return
        if not urls:
            return
        if not self.output_folder:
            QMessageBox.warning(self, "Error", "Please select a download folder.")
            return

        fmt = self.format_combo.currentText()
        cookies_browser = self.cookie_combo.currentData()
        subtitles = self.subs_check.isChecked()
        sublangs = self.subs_lang.currentData() or "en"
        self.settings.setValue("format", fmt)
        self.settings.setValue("cookies_browser", cookies_browser)

        # Reset the rows we're about to (re)download back to "pending".
        target = set(urls)
        for i in range(self.link_list.count()):
            it = self.link_list.item(i)
            if self._item_url(it) in target:
                self._set_item_state(it, "pending")

        self.log_view.clear()
        if any("youtu" in u for u in urls) and cookies_browser == "none":
            self.log("Note: YouTube with cookies set to 'none' — you may only "
                     "get 360p or hit 'not a bot' errors. Pick a browser above.")
        self.progress_bar.setValue(0)
        self._cancelled = False
        self._set_busy(True)

        self.worker = DownloadWorker(urls, fmt, self.output_folder, cookies_browser,
                                     subtitles=subtitles, sublangs=sublangs)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self._set_progress)
        self.worker.item_started.connect(self._on_item_started)
        self.worker.item_finished.connect(self._on_item_finished)
        self.worker.all_finished.connect(self._on_all_finished)
        self.worker.start()

    def cancel_download(self):
        if self.worker and self.worker.isRunning():
            self.log("Cancelling after the current file…")
            self.cancel_btn.setEnabled(False)
            self._cancelled = True
            self.worker.cancel()

    def _on_item_started(self, idx, total, url):
        self.progress_bar.setValue(0)
        item = self._find_item(url)
        if item is not None:
            self._set_item_state(item, "downloading")
            self.link_list.scrollToItem(item)
        self.log(f"[{idx}/{total}] {url}")

    def _on_item_finished(self, url, success, err, filepath=""):
        item = self._find_item(url)
        if success:
            if item is not None:
                if filepath:
                    item.setData(self._ROLE_FILE, filepath)
                self._set_item_state(item, "done",
                                     tip=f"{url}\nDouble-click to play" if filepath else url)
            self.log("  ✓ done")
        else:
            # Show the most relevant line of the error, not the whole traceback.
            short = err.strip().splitlines()[-1] if err.strip() else "unknown error"
            if item is not None:
                self._set_item_state(item, "failed", tip=f"{url}\n{short}")
            self.log(f"  ✖ FAILED: {short}")
            low = err.lower()
            if "dpapi" in low or ("cookies" in low and "decrypt" in low):
                self.log("    → Couldn't read that browser's cookies (Chromium "
                         "app-bound encryption). Try 'firefox' in the cookies "
                         "dropdown, or fully close the browser and retry.")
            elif "not a bot" in low or "sign in to confirm" in low:
                self.log("    → YouTube bot-check. Set the cookies dropdown to a "
                         "browser you're logged into YouTube with (try firefox).")

    def _on_all_finished(self, ok, fail):
        self._set_busy(False)
        self._refresh_retry_button()
        if self._cancelled:
            # Reset the row that was mid-download so it doesn't sit on ↓ forever,
            # and skip the success popup / chime that a normal finish would show.
            for i in range(self.link_list.count()):
                it = self.link_list.item(i)
                if it.data(self._ROLE_STATE) == "downloading":
                    self._set_item_state(it, "pending")
            self._set_pill(self.state_pill, "Cancelled", "warn")
            self.log("\nCancelled.")
            return
        self.progress_bar.setValue(100 if fail == 0 else self.progress_bar.value())
        self.log(f"\nFinished. {ok} succeeded, {fail} failed.")
        if self.notify_check.isChecked():
            self._notify_done(ok, fail)
        if fail == 0:
            self._set_pill(self.state_pill, f"Done · {ok} ok", "ok")
            QMessageBox.information(
                self, "Success", f"All {ok} download(s) completed successfully!"
            )
        else:
            self._set_pill(self.state_pill, f"Done · {ok} ok / {fail} fail", "warn")
            QMessageBox.warning(
                self, "Completed with errors",
                f"{ok} succeeded, {fail} failed.\nSee the log for details."
            )

    def _refresh_retry_button(self):
        # Enabled whenever any row is in the failed state. A running batch keeps
        # it disabled via _set_busy(True); this is only refreshed once a batch
        # ends, so it must not gate on the (racy) worker.isRunning() flag.
        has_failed = any(
            self.link_list.item(i).data(self._ROLE_STATE) == "failed"
            for i in range(self.link_list.count()))
        self.retry_btn.setEnabled(has_failed)

    # === Completion notification (toast + chime) ===
    def _ensure_tray(self):
        if self._tray is None and QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(QIcon(make_app_pixmap(256)), self)
            self._tray.setToolTip("Video / Audio Downloader")
            self._tray.activated.connect(
                lambda *_: (self.showNormal(), self.raise_(), self.activateWindow()))
            self._tray.show()
        return self._tray

    def _notify_done(self, ok, fail):
        title = "Downloads complete" if fail == 0 else "Downloads finished with errors"
        msg = f"{ok} succeeded" + (f", {fail} failed" if fail else "") + "."
        tray = self._ensure_tray()
        if tray is not None:
            icon = (QSystemTrayIcon.Information if fail == 0
                    else QSystemTrayIcon.Warning)
            tray.showMessage(title, msg, icon, 5000)
        if sys.platform == "win32":
            try:
                import winsound
                winsound.MessageBeep(
                    winsound.MB_OK if fail == 0 else winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

    def _set_busy(self, busy):
        self.download_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.update_btn.setEnabled(not busy)
        self.cookie_combo.setEnabled(not busy)
        self.format_combo.setEnabled(not busy)
        if busy:
            self.retry_btn.setEnabled(False)
            self._set_pill(self.state_pill, "Working…", "busy")
            self._pulse_anim.start()
        else:
            self._set_pill(self.state_pill, "Ready", "idle")
            self._pulse_anim.stop()
            self._progress_glow.setBlurRadius(2)

    # === yt-dlp self-update ===
    def update_ytdlp(self):
        # Packaged build: download wheels straight from PyPI (no pip/Python on
        # the user's machine). Dev run: the classic pip upgrade.
        if IS_FROZEN:
            self._start_lib_update(manual=True)
            return
        if self.updater and self.updater.isRunning():
            return
        self.log("Updating yt-dlp…")
        self.update_btn.setEnabled(False)
        self.updater = UpdateWorker()
        self.updater.log.connect(self.log)
        self.updater.done.connect(self._on_update_done)
        self.updater.start()

    def _start_lib_update(self, manual):
        """Frozen-build updater. manual=True is the button (chatty + dialog);
        manual=False is the silent check on launch."""
        if self._lib_updater and self._lib_updater.isRunning():
            return
        if manual:
            self.log("Checking for a yt-dlp update…")
            self.update_btn.setEnabled(False)
        try:
            current = yt_dlp.version.__version__
        except Exception:
            current = ""
        self._lib_updater = LibUpdateWorker(current)
        self._lib_updater.log.connect(self.log)
        self._lib_updater.done.connect(
            lambda changed, msg, m=manual: self._on_lib_update_done(changed, msg, m))
        self._lib_updater.start()

    def _on_lib_update_done(self, changed, msg, manual):
        self.update_btn.setEnabled(True)
        if changed:
            self.log("✓ yt-dlp %s downloaded — it applies next time you open "
                     "the app." % msg)
            if manual:
                QMessageBox.information(
                    self, "Update ready",
                    "yt-dlp %s was downloaded.\nIt will be applied automatically "
                    "the next time you open the app." % msg)
        elif manual:
            self.log("• " + msg)
            QMessageBox.information(self, "yt-dlp update", msg)
        else:
            flog("auto-update: " + msg)

    def _on_update_done(self, success):
        self.update_btn.setEnabled(True)
        if success:
            self.log(
                "✓ yt-dlp updated. Restart the app for the new version "
                "to take effect."
            )
            QMessageBox.information(
                self, "Update complete",
                "yt-dlp was updated.\nPlease restart the app to load it."
            )
        else:
            self.log("✖ yt-dlp update failed (see log above).")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        if self._thumb_worker and self._thumb_worker.isRunning():
            self._thumb_worker.cancel()
            self._thumb_worker.wait(2000)
        if self.updater and self.updater.isRunning():
            self.updater.cancel()
            self.updater.wait(3000)
        if self._lib_updater and self._lib_updater.isRunning():
            self._lib_updater.cancel()
            self._lib_updater.wait(3000)
        if self._tray is not None:
            self._tray.hide()
        # Shut down the PO-token server we started (leave a pre-existing one).
        proc = getattr(self, "_pot_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                pass
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(make_app_pixmap(256)))
    window = DownloaderUI()
    window.show()
    sys.exit(app.exec())
