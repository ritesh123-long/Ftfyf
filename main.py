#!/usr/bin/env python3
# app.py — yt-dlp + Flask downloader (saves MP3 & auto-deletes after 1 hour)

import os
import re
import time
import shutil
import signal
import pathlib
import tempfile
import threading
from flask import Flask, send_file, request, abort, jsonify
from yt_dlp import YoutubeDL, DownloadError

APP_ROOT = pathlib.Path(__file__).parent.resolve()
DOWNLOAD_DIR = APP_ROOT / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# 1 hour in seconds
ONE_HOUR = 60 * 60

# quality -> bitrate mapping for ffmpeg conversion
QUALITY_MAP = {
    "low": "64k",
    "medium": "128k",
    "high": "320k"
}

# Default yt-dlp options (we will override format per attempt)
BASE_YTDL_OPTS = [
    "--no-playlist",
    "--no-warnings",
    "--no-call-home",
    "--no-config",
    "--rm-cache-dir",
    "--geo-bypass",
]

# If you want more verbose debug logging, set True
DEBUG_VERBOSE = False

def write_cookies_from_env():
    """
    If COOKIES_CONTENT env var exists (full cookies.txt content),
    write it into COOKIES_FILE (default ./cookies.txt).
    """
    content = os.environ.get("COOKIES_CONTENT")  # full cookies.txt content
    if not content:
        return None
    cookies_file = os.environ.get("COOKIES_FILE", "cookies.txt")
    path = APP_ROOT / cookies_file
    try:
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)
        app.logger.info("Wrote cookies file from COOKIES_CONTENT to %s", path)
        return str(path)
    except Exception as e:
        app.logger.warning("Could not write cookies file: %s", e)
        return None

# write cookies at startup if provided
COOKIES_PATH = write_cookies_from_env()
if not COOKIES_PATH and os.environ.get("USE_COOKIES", "").lower() == "true":
    # If user requested cookies but didn't provide content, check file exists
    cf = os.environ.get("COOKIES_FILE", "cookies.txt")
    if (APP_ROOT / cf).exists():
        COOKIES_PATH = str((APP_ROOT / cf).resolve())
        app.logger.info("Using existing cookies file: %s", COOKIES_PATH)

def schedule_delete(filepath, delay=ONE_HOUR):
    """Schedule a file deletion after `delay` seconds."""
    def _del():
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                app.logger.info("Auto-deleted %s", filepath)
        except Exception as e:
            app.logger.warning("Failed to delete %s: %s", filepath, e)
    t = threading.Timer(delay, _del)
    t.daemon = True
    t.start()

def sanitize_filename(name: str) -> str:
    # remove bad characters, keep it short
    name = re.sub(r'[\\/*?:"<>|]', "-", name)
    name = name.strip()
    return name[:120]

def make_ydl_opts(output_path, audio_bitrate, cookies_path=None, fmt=None):
    """
    Build yt-dlp options dict for YoutubeDL.
    - output_path: full file path for final mp3
    - audio_bitrate: like '128k'
    - cookies_path: optional cookies file path
    - fmt: yt-dlp format string (e.g. 'bestaudio[ext=m4a]/bestaudio')
    """
    postprocessors = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(int(audio_bitrate.replace("k","")))
        }
    ]
    opts = {
        "format": fmt or "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(output_path),
        "postprocessors": postprocessors,
        "noplaylist": True,
        "quiet": not DEBUG_VERBOSE,
        "no_warnings": True,
        "ignoreerrors": False,
        "no_call_home": True,
        "prefer_insecure": False,
        "rm_cachedir": True,
    }
    if cookies_path:
        opts["cookiefile"] = cookies_path
    # pass additional flags
    opts["compat_opts"] = BASE_YTDL_OPTS
    return opts

def run_yt_dlp_attempts(video_url, file_base, bitrate, cookies_path=None):
    """
    Try multiple format fallbacks. Returns (success, filepath, debug_text)
    debug_text contains combined stderr/info on failure.
    """
    formats_to_try = [
        "bestaudio[ext=m4a]/bestaudio",
        "bestaudio[protocol^=https]/bestaudio",
        "bestaudio[ext=webm]/bestaudio",
        "bestaudio"
    ]
    debug_parts = []
    for fmt in formats_to_try:
        # create a temporary output template like /tmp/base-<fmt>.%(ext)s
        # yt-dlp will produce a temporary file like .part and convert to .mp3 via postprocessor
        outtmpl = str((DOWNLOAD_DIR / (file_base + "-" + fmt.replace("/", "_") + ".%(ext)s")).resolve())
        opts = make_ydl_opts(outtmpl, bitrate, cookies_path=cookies_path, fmt=fmt)
        try:
            app.logger.info("yt-dlp attempt fmt=%s", fmt)
            with YoutubeDL(opts) as ydl:
                ydl.download([video_url])
            # find the generated mp3 file: search DOWNLOAD_DIR for file_base and .mp3 suffix
            # we expect a file named something like file_base-... .mp3
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(file_base) and f.endswith(".mp3"):
                    fp = str((DOWNLOAD_DIR / f).resolve())
                    return True, fp, ""
            debug_parts.append(f"Attempt {fmt} completed but .mp3 not found in downloads.")
        except DownloadError as de:
            txt = f"yt-dlp DownloadError fmt={fmt}: {de}\n"
            app.logger.warning(txt)
            debug_parts.append(txt)
        except Exception as e:
            txt = f"yt-dlp Exception fmt={fmt}: {e}\n"
            app.logger.warning(txt)
            debug_parts.append(txt)
        # cleanup partial matches for this file_base to avoid conflicts
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(file_base) and (f.endswith(".part") or f.endswith(".temp") or f.endswith(".m4a") or f.endswith(".webm")):
                try:
                    os.remove(DOWNLOAD_DIR / f)
                except Exception:
                    pass
    return False, None, "\n".join(debug_parts)


@app.route("/", methods=["GET"])
def index():
    return "yt-dlp Flask downloader running. Use /high/id=ID or /high/url=URL"

@app.route("/<quality>/<param>", methods=["GET"])
def download_v1(quality, param):
    """
    Backward-compatible simple route: param can be 'id=...' or 'url=...'
    But we'll recommend the expressive route below.
    """
    return download_handler(quality, param)

@app.route("/<quality>/<path:kv>", methods=["GET"])
def download_handler(quality, kv):
    """
    Main handler that accepts:
      - /<quality>/id=VIDEOID
      - /<quality>/url=<encoded or raw URL>
    We use Flask path converter so URL with slashes is accepted.
    """
    try:
        # parse kv like "id=abc" or "url=https://..."
        if "=" not in kv:
            return abort(400, "Invalid param. Use id=VIDEOID or url=FULL_URL")
        key, value = kv.split("=", 1)
        key = key.lower()

        q = quality.lower()
        if q not in QUALITY_MAP:
            q = "high"
        bitrate = QUALITY_MAP[q]

        if key == "id":
            video_url = f"https://www.youtube.com/watch?v={value}"
        elif key == "url":
            # try decode once (value may be percent-encoded)
            try:
                from urllib.parse import unquote
                video_url = unquote(value)
            except Exception:
                video_url = value
        else:
            return abort(400, "Invalid key. Use id= or url=")

        # test a bit: if value looks like full url even with id=, allow it
        if key == "id" and (value.startswith("http://") or value.startswith("https://")):
            video_url = value

        # Build friendly filename base
        # Attempt to get title using yt-dlp extract_info (no download) to name file
        file_base = None
        try:
            ydl_info_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
            if COOKIES_PATH:
                ydl_info_opts["cookiefile"] = COOKIES_PATH
            with YoutubeDL(ydl_info_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
                title = info.get("title") or None
                if title:
                    file_base = sanitize_filename(title)
        except Exception as e:
            # ignore metadata errors
            app.logger.debug("metadata error: %s", e)

        if not file_base:
            # fallback: use timestamp + id-ish
            safe = re.sub(r'[^A-Za-z0-9_-]', '_', video_url)[0:40]
            file_base = f"{safe}-{int(time.time())}"

        # if a file for same base+quality already exists, serve it immediately
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(file_base) and f.endswith(".mp3") and f"-{q}-" in f:
                fp = str((DOWNLOAD_DIR / f).resolve())
                app.logger.info("Serving cached file %s", fp)
                schedule_delete(fp)  # reset deletion timer once served
                return send_file(fp, as_attachment=True, download_name=os.path.basename(fp))

        # run yt-dlp attempts (will convert to mp3 using FFmpeg)
        success, filepath, debug_text = run_yt_dlp_attempts(video_url, file_base + f"-{q}", bitrate, cookies_path=COOKIES_PATH)
        if success:
            schedule_delete(filepath)
            return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))
        else:
            # conversion failed — return debug info
            msg = "Conversion failed.\n\n" + debug_text
            if "410" in debug_text or "403" in debug_text or "age" in debug_text.lower():
                msg += ("\n\nHint: video may be age/region-restricted or removed. "
                        "If restricted, provide cookies: set USE_COOKIES=true and COOKIES_CONTENT (cookie file content) in environment.")
            return abort(500, msg)
    except Exception as e:
        app.logger.exception("Unexpected error in handler")
        return abort(500, f"Server error: {e}")

if __name__ == "__main__":
    # helpful message about ffmpeg presence
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        app.logger.warning("ffmpeg binary not found in PATH. yt-dlp needs ffmpeg to convert to mp3.")
        app.logger.warning("On Replit you may install ffmpeg via apt-get in the shell: sudo apt-get update && sudo apt-get install -y ffmpeg")
    else:
        app.logger.info("ffmpeg found at: %s", ffmpeg_path)

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))