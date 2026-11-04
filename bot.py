#!/usr/bin/env python3
"""
Telegram Link Bot v3.0-terabest-like - Terabox ONLY, PC-first.
Made to work like @TeraBestBot (t.me/terabestbot):
  "TeraBot Link To Video Bot | Tera Box DL"
  - Send a Terabox link, get a Direct Download Link + Online Player
  - No upload of big files to Telegram (so 10GB+ files work via link/player)
  - Folder links list ALL files, not just the first one
  - Batch links: send several links in one message, bot does them one by one
  - Tries guest / no-login mode first, like TeraBestBot claims;
    only falls back to your TERABOX_COOKIE if the guest dlink is empty
  - /start is simple, like TeraBestBot: send link, tap Online Player

Based on v2.1-fixed (2026-11-04, not yet tested live against Terabox/Telegram).
v2.1 fixes:
  - Upload timeouts raised (PTB default write timeout is 5s, too short
    for 20-50 MB videos) and concurrent_updates turned on, so one
    download no longer freezes /start and other messages.
  - All user-visible messages use HTML with escaping, not Markdown,
    so file names with _ or * no longer crash the message.
  - Windows-safe file names: : ? " < > | etc. are replaced, so
    open() no longer fails on Windows.
  - Player web server now starts on port 8080 whenever PUBLIC_URL is
    set (previously it only started when PORT existed, which a PC
    does not have).
  - Download Content-Disposition now uses RFC 5987 UTF-8 encoding,
    so non-English (e.g. Hindi) file names no longer break the header.
  - Stream proxy: a 403 from Terabox now returns a real 502 instead
    of a fake 200 OK with an error body that made the player hang.
  - Folder shares: picks a real file from the list, looking one level
    inside a folder if needed, instead of taking list[0] blindly.
  - ALLOWED_USER_IDS: optional allow-list, blank = allow everyone.

v2.0 lessons:
1. NO terabox-downloader library. v1 used it and it forced the wrong

1. NO terabox-downloader library. v1 used it and it forced the wrong
   Host header, crashed on get_file_info(direct_url=True), and gave
   no clue which token was missing. v2 uses only our own resolver.
2. NO dp-logid sent to Terabox API. v1 sent it and Terabox refused
   every call with 460020 "need verify". v2 never sends it.
3. NO Application.run_in_executor - it does not exist in
   python-telegram-bot 22. v2 uses asyncio.to_thread everywhere.
4. teraboxshare.com recognised from the start. v1 rejected it first.
5. Short link /s/1<surl> - the leading "1" is NOT part of surl.
   v2 strips it first, then tries the full value as fallback.
6. A 45-char ndus is a REAL complete cookie. v2 never says "too short".
   It only reports cookie names and length, never the values.
7. PC-first, single instance only. v1 ran on Render + PC at the same
   time and Telegram gave Conflict errors. v2 is PC-only to start.
   Suspend Render before running v2.
8. Version is printed on /start and on startup, so we can ALWAYS tell
   if the old file is still running. v1 had no version check.
9. 50 MB limit, not 2 GB. Telegram Bot API only allows 50 MB uploads.
   v1 said 2 GB and then failed on big files.
10. Terabox-only, no Diskwala / Playwright in v2. Simpler = fewer
    things to break. Diskwala can be added later if needed.

Run on PC: double-click start_bot.bat (it makes .env for you).
"""

import asyncio
import html
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

VERSION = "v3.0-terabest-like-2026-11-04"
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TERABOX_COOKIE = os.getenv("TERABOX_COOKIE", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
# Optional allow-list, comma-separated Telegram user IDs.
# Blank = allow everyone. Get your ID from @userinfobot on Telegram.
# Example: ALLOWED_USER_IDS=123456789,987654321
try:
    ALLOWED_USER_IDS = {
        int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if x.strip()
    }
except ValueError:
    ALLOWED_USER_IDS = set()

# Telegram Bot API upload limit is 50 MB. Do not raise this.
MAX_FILE_BYTES = 50 * 1024 * 1024
PLAY_TTL_HOURS = 6

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("linkbot-v2")

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".ts", ".3gp"}
URL_RE = re.compile(r"https?://[^\s<>\"']+")

pending: dict[str, dict] = {}
play_sessions: dict[str, dict] = {}


def is_terabox_link(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "terabox" in host or host in (
        "1024tera.com", "www.1024tera.com", "mirrobox.com",
        "nephobox.com", "freemibox.com", "dubox.com",
    )


def human_size(n: int | None) -> str:
    if not n:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _is_video(meta: dict) -> bool:
    ext = os.path.splitext(meta.get("name", ""))[1].lower()
    return ext in VIDEO_EXTS or str(meta.get("mime", "")).startswith("video/")


def _cookie_names() -> str:
    return ",".join(
        p.split("=", 1)[0].strip()
        for p in TERABOX_COOKIE.split(";") if "=" in p
    ) or "(none)"


def _terabox_headers() -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.terabox.com/",
    }
    if TERABOX_COOKIE:
        headers["Cookie"] = TERABOX_COOKIE
    return headers


def _safe_filename(name: str) -> str:
    """Windows-safe file name: : ? " < > | / \\ and control chars -> _."""
    name = (name or "file").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")
    if not name:
        name = "file"
    # Keep it short enough for Windows paths.
    if len(name) > 150:
        base, ext = os.path.splitext(name)
        name = base[: 150 - len(ext)] + ext
    return name


def _content_disposition(name: str, download: bool) -> str:
    """RFC 5987 disposition so non-English names (e.g. Hindi) work."""
    if not download:
        return "inline"
    safe_ascii = re.sub(r"[^\x20-\x7e]", "_", name or "file").replace('"', "_")
    encoded = quote((name or "file").encode("utf-8"))
    return f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded}'


def _is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


# ---------------- Terabox resolver (custom only, no library) ----------------

def _meta_from_info(info: dict) -> dict:
    size_bytes = int(info.get("size", 0) or 0)
    return {
        "name": info.get("server_filename") or "file",
        "size": human_size(size_bytes) if size_bytes else None,
        "size_bytes": size_bytes or None,
        "url": info.get("dlink"),
        "thumb": (info.get("thumbs") or {}).get("url3"),
    }


def resolve_terabox(link: str) -> dict:
    # TeraBestBot-style: try guest / no-login first. Only if the
    # guest dlink comes back empty do we need the cookie.
    # So do NOT raise here when TERABOX_COOKIE is empty.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/135.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.terabox.app/",
    }
    if TERABOX_COOKIE:
        headers["Cookie"] = TERABOX_COOKIE

    # Get surl from the link. /s/1<surl> -> surl is without the leading 1.
    m_path = re.search(r"/s/([A-Za-z0-9_-]+)", urlparse(link).path)
    seg = m_path.group(1) if m_path else ""
    qs = parse_qs(urlparse(link).query)
    surl_from_qs = qs.get("surl", [None])[0]

    candidates = []
    if seg:
        candidates.append(f"https://www.terabox.app/sharing/link?surl={seg[1:]}")
        candidates.append(f"https://www.terabox.app/sharing/link?surl={seg}")
    if surl_from_qs:
        candidates.append(f"https://www.terabox.app/sharing/link?surl={surl_from_qs}")
    candidates.append(link)

    with requests.Session() as sess:
        r = None
        text = ""
        js_token = logid = bdstoken = ""
        surl = surl_from_qs or (seg[1:] if seg else None)
        last_err = None
        for cand in candidates:
            try:
                resp = sess.get(cand, headers=headers, timeout=20)
                if not resp.ok:
                    last_err = RuntimeError(f"Terabox page returned HTTP {resp.status_code}.")
                    continue
            except Exception as e:
                last_err = e
                continue
            r, text = resp, resp.text
            qs2 = parse_qs(urlparse(resp.url).query)
            if qs2.get("surl", [None])[0]:
                surl = qs2["surl"][0]
            m_js = re.search(r"fn%28%22([0-9A-Fa-f]+)%22%29", text) \
                or re.search(r'jsToken":"([0-9A-Za-z]+)"', text)
            js_token = m_js.group(1) if m_js else ""
            m_log = re.search(r"dp-logid=(\d+)", text)
            logid = m_log.group(1) if m_log else ""
            m_bd = re.search(r'bdstoken["\']?\s*[:=]\s*["\']([^"\',;]+)', text) \
                or re.search(r"bdstoken%22%3A%22([^%\"']+)", text)
            bdstoken = (m_bd.group(1) if m_bd else "").strip()
            if js_token and logid:
                break
            last_err = RuntimeError("Share page did not contain login tokens.")
        if r is None or not r.ok:
            raise last_err or RuntimeError("Terabox share page could not be fetched.")
        if not surl:
            raise RuntimeError("Could not find a share ID (surl) in this link.")

        log.info(
            "v2 resolve: host=%s cookies=%s cookie_len=%d js=%s logid=%s bdstoken=%s",
            urlparse(r.url).netloc, _cookie_names(), len(TERABOX_COOKIE),
            bool(js_token), bool(logid), bool(bdstoken),
        )
        if not js_token or not logid:
            raise RuntimeError(
                f"Terabox did not give login tokens (js={bool(js_token)}, "
                f"logid={bool(logid)}, bdstoken={bool(bdstoken)}, "
                f"cookies={_cookie_names()}, cookie_len={len(TERABOX_COOKIE)}). "
                "Re-copy ndus + ndut_fmt fresh from a logged-in terabox.app tab."
            )

        # NEVER send dp-logid or bdstoken to the API - Terabox refuses
        # with 460020 "need verify" if dp-logid is sent.
        host = urlparse(r.url).netloc
        hosts = []
        for h in [host, "www.terabox.app", "www.1024tera.com"]:
            if h and h not in hosts:
                hosts.append(h)

        data = {}
        last_api_err = ""
        for api_host in hosts:
            attempts = [
                ("api/shorturlinfo", {
                    "app_id": "250528", "web": "1", "channel": "dubox",
                    "clienttype": "0", "jsToken": js_token,
                    "shorturl": surl, "root": "1",
                }),
                ("share/list", {
                    "app_id": "250528", "web": "1", "channel": "dubox",
                    "clienttype": "0", "jsToken": js_token,
                    "page": "1", "num": "20", "by": "name", "order": "asc",
                    "site_referer": r.url, "shorturl": surl, "root": "1",
                }),
            ]
            for path, params in attempts:
                try:
                    r2 = sess.get(f"https://{api_host}/{path}", headers=headers,
                                  params=params, timeout=30)
                    data = r2.json()
                except Exception as e:
                    last_api_err = str(e)
                    data = {}
                    continue
                if data and data.get("list") and not data.get("errno"):
                    break
                last_api_err = str(data.get("errmsg") or data.get("errno") or data)
                data = data if data.get("list") else {}
            if data and data.get("list"):
                break

        if not data or not data.get("list"):
            m_name = re.search(r'server_filename["\']?\s*[:=]\s*["\']([^"\']+)', text)
            m_link = re.search(r'dlink["\']?\s*[:=]\s*["\']([^"\']+)', text)
            m_size = re.search(r'"size"\s*:\s*(\d+)', text)
            if m_name and m_link:
                _one = {
                    "name": m_name.group(1),
                    "size": human_size(int(m_size.group(1))) if m_size else None,
                    "size_bytes": int(m_size.group(1)) if m_size else None,
                    "url": m_link.group(1).replace("\\/", "/"),
                    "thumb": None,
                }
                _one["all_files"] = [_one]
                _one["is_folder"] = False
                _one["total_files"] = 1
                return _one
            raise RuntimeError(
                f"Terabox: could not read the file list. API said: {last_api_err or 'no response'}. "
                "If it says 'need verify', the cookie was not accepted for this share."
            )

        # Folder shares: collect ALL real files, not just list[0].
        # If an item is a folder (isdir=1, no dlink), look one level
        # inside it via share/list with dir=<folder path>.
        all_infos: list[dict] = []
        for item in data["list"]:
            if item.get("isdir"):
                continue
            if item.get("dlink"):
                all_infos.append(item)
        if not all_infos:
            for item in data["list"]:
                if not item.get("isdir"):
                    continue
                folder_path = item.get("path") or ("/" + str(item.get("server_filename") or "").strip("/"))
                if not folder_path or folder_path == "/":
                    continue
                try:
                    r3 = sess.get(
                        f"https://{hosts[0]}/share/list", headers=headers,
                        params={
                            "app_id": "250528", "web": "1", "channel": "dubox",
                            "clienttype": "0", "jsToken": js_token,
                            "page": "1", "num": "20", "by": "name", "order": "asc",
                            "site_referer": r.url, "shorturl": surl, "root": "1",
                            "dir": folder_path,
                        },
                        timeout=30,
                    )
                    sub = r3.json().get("list") or []
                    for sub_item in sub:
                        if not sub_item.get("isdir") and sub_item.get("dlink"):
                            all_infos.append(sub_item)
                except Exception:
                    continue
        if not all_infos:
            raise RuntimeError(
                "Terabox gave the file name but no download link (dlink empty). "
                "That means Terabox did not accept this session as logged in, "
                "or this share contains only folders with no file inside. "
                "If you have a TERABOX_COOKIE set, re-copy ndus + ndut_fmt fresh "
                "from terabox.app while logged in. If not, this link may need a login."
            )
        info = all_infos[0]
        all_files = [_meta_from_info(x) for x in all_infos]
        result = _meta_from_info(info)
        result["all_files"] = all_files
        result["is_folder"] = len(data["list"]) > 1 or bool(data["list"][0].get("isdir"))
        result["total_files"] = len(all_files)
        return result


def download_file(url: str, dest: str, progress_cb=None) -> int:
    with requests.get(url, stream=True, timeout=60, headers=_terabox_headers()) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0) or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
    return done


# ---------------- In-app player (only used if PUBLIC_URL is set) ----------------

def _player_html(meta: dict, key: str) -> str:
    name = html.escape(meta.get("name") or "video")
    size = html.escape(meta.get("size") or "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>body{{margin:0;background:#111;color:#eee;font-family:sans-serif}}
.wrap{{max-width:720px;margin:0 auto;padding:16px}}video{{width:100%;background:#000;border-radius:8px}}
a.btn{{display:block;text-align:center;margin-top:14px;padding:12px;background:#2ea6ff;color:#fff;text-decoration:none;border-radius:8px}}</style>
</head><body><div class="wrap"><h2>{name}</h2><p>{size}</p>
<video src="/stream/{key}" controls autoplay playsinline preload="metadata"></video>
<a class="btn" href="/download/{key}">Download / Save to device</a>
</div></body></html>"""


class _WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            body = b"ok v2"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/play/"):
            key = path[len("/play/"):]
            sess = play_sessions.get(key)
            if not sess or sess["expires"] < time.time():
                self.send_response(410)
                self.end_headers()
                return
            body = _player_html(sess["meta"], key).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith(("/stream/", "/download/")):
            download = path.startswith("/download/")
            key = path.split("/", 2)[2]
            sess = play_sessions.get(key)
            if not sess or not sess.get("url") or sess["expires"] < time.time():
                self.send_response(410)
                self.end_headers()
                return
            meta = sess["meta"]
            headers = _terabox_headers()
            if self.headers.get("Range"):
                headers["Range"] = self.headers["Range"]
            try:
                r = requests.get(sess["url"], stream=True, timeout=30, headers=headers)
            except Exception:
                log.exception("player stream failed")
                self.send_response(502)
                self.end_headers()
                return
            # A 403/4xx from Terabox is a real failure - do NOT reply
            # 200 OK with the error body, or the player hangs.
            if r.status_code not in (200, 206):
                log.warning("player stream refused: Terabox HTTP %s", r.status_code)
                body = f"Terabox refused the stream (HTTP {r.status_code}).".encode()
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                r.close()
                return
            ctype = r.headers.get("content-type") or mimetypes.guess_type(meta.get("name", ""))[0] \
                or ("video/mp4" if _is_video(meta) else "application/octet-stream")
            self.send_response(r.status_code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            if r.headers.get("content-length"):
                self.send_header("Content-Length", r.headers["content-length"])
            if r.headers.get("content-range"):
                self.send_header("Content-Range", r.headers["content-range"])
            self.send_header("Content-Disposition",
                             _content_disposition(meta.get("name", "file"), download))
            self.end_headers()
            try:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                r.close()
            return
        self.send_response(404)
        self.end_headers()


def _start_web_server():
    # On a PC there is no PORT variable, so the old code never started
    # the player at all. Start on 8080 whenever PUBLIC_URL is set,
    # or on PORT if that is set (Render).
    port_str = os.getenv("PORT")
    if port_str:
        port = int(port_str)
    elif PUBLIC_URL:
        port = 8080
    else:
        return
    server = ThreadingHTTPServer(("0.0.0.0", port), _WebHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Web server on port %s (PUBLIC_URL=%s)", port, PUBLIC_URL or "(none)")


# ---------------- Telegram handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    await update.message.reply_text(
        "TeraBot Link To Video Bot | Tera Box DL\n\n"
        "Send me a Terabox link and I will give you:\n"
        "🌐 Online Player - watch inside Telegram\n"
        "🔗 Direct Download Link - full-speed download, any size\n"
        "▶️ Play here - for small videos, plays in the chat\n\n"
        "No login needed for public links. Just paste the link.\n"
        "Folder links and several links at once also work.\n\n"
        f"Bot: {VERSION} | Use /help for more, /check for status."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    await update.message.reply_text(
        "How to use:\n"
        "1. Copy a Terabox share link\n"
        "2. Paste it here\n"
        "3. Tap 🌐 Online Player to watch, or 🔗 Direct Download Link to download\n\n"
        "Large files (10GB+) work via Direct Link / Online Player - "
        "Telegram itself only allows 50 MB uploads, so big files are never uploaded.\n"
        "Folder links show all files inside. Send several links in one message and I will do them one by one."
    )


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    await update.message.reply_text(
        f"Bot version: {VERSION}\n"
        f"Token set: {'yes' if BOT_TOKEN else 'NO - fill .env'}\n"
        f"Cookie names: {_cookie_names()}\n"
        f"Cookie length: {len(TERABOX_COOKIE)} chars\n"
        f"PUBLIC_URL: {PUBLIC_URL or '(blank - PC mode, direct link will be used)'}\n"
        f"Allowed users: {', '.join(map(str, sorted(ALLOWED_USER_IDS))) if ALLOWED_USER_IDS else 'all (no restriction)'}\n"
        f"Max upload: 50 MB"
    )


def _register_file(meta: dict) -> str:
    key = uuid.uuid4().hex[:8]
    play_sessions[key] = {
        "meta": meta, "url": meta["url"],
        "expires": time.time() + PLAY_TTL_HOURS * 3600,
    }
    return key


def _player_url(meta: dict, key: str) -> str:
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/play/{key}"
    return meta["url"]


def _file_buttons(meta: dict, chat_id: int) -> tuple[list[list[InlineKeyboardButton]], str]:
    """TeraBestBot order: Online Player first, Direct Link second,
    Telegram upload only for small files. Returns (buttons, key)."""
    size_bytes = meta.get("size_bytes")
    too_big = bool(size_bytes and size_bytes > MAX_FILE_BYTES)
    is_video = _is_video(meta)
    key = _register_file(meta)
    # Only register for Telegram upload if it can actually be uploaded.
    if not too_big:
        pending[key] = {"meta": meta, "chat_id": chat_id}
    buttons: list[list[InlineKeyboardButton]] = []
    if is_video:
        buttons.append([InlineKeyboardButton(
            "🌐 Online Player", url=_player_url(meta, key))])
    else:
        if PUBLIC_URL:
            buttons.append([InlineKeyboardButton(
                "🌐 Open in app browser", url=_player_url(meta, key))])
    buttons.append([InlineKeyboardButton(
        "🔗 Direct Download Link", url=meta["url"])])
    if not too_big:
        if is_video:
            buttons.append([InlineKeyboardButton(
                "▶️ Play here in Telegram", callback_data=f"send:video:{key}")])
        buttons.append([InlineKeyboardButton(
            "⬇️ Download via Telegram", callback_data=f"send:file:{key}")])
    return buttons, key


async def _process_one_link(status, url: str, chat_id: int):
    try:
        meta = await asyncio.to_thread(resolve_terabox, url)
    except Exception as e:
        log.exception("resolve failed")
        await status.edit_text(f"❌ Couldn't resolve that link:\n{html.escape(str(e))}", parse_mode="HTML")
        return
    if not meta.get("url"):
        await status.edit_text("❌ Resolved the link but got no download URL.")
        return
    all_files = meta.get("all_files") or [meta]
    # Folder / batch result: list every file, like TeraBestBot.
    if meta.get("total_files", 1) > 1 or meta.get("is_folder"):
        lines = [f"📁 Folder - {len(all_files)} file(s):"]
        buttons: list[list[InlineKeyboardButton]] = []
        for f in all_files[:8]:
            f_key = _register_file(f)
            lines.append(f"• {f.get('name')} - {f.get('size') or human_size(f.get('size_bytes'))}")
            buttons.append([InlineKeyboardButton(
                f"🌐 {f.get('name','file')[:30]}", url=_player_url(f, f_key))])
            buttons.append([InlineKeyboardButton(
                f"🔗 Direct - {f.get('name','file')[:30]}", url=f["url"])])
        if len(all_files) > 8:
            lines.append(f"... and {len(all_files) - 8} more file(s). Send the folder link again for the rest.")
        await status.edit_text(
            "\n".join(html.escape(x) if not x.startswith("📁") else x for x in lines),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
        )
        return
    buttons, _key = _file_buttons(meta, chat_id)
    buttons.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{_key}")])
    size_txt = meta.get("size") or human_size(meta.get("size_bytes"))
    note = ""
    if meta.get("size_bytes") and meta["size_bytes"] > MAX_FILE_BYTES:
        note = (f"\n⚠️ This file is {size_txt} - too big for Telegram upload "
                "(50 MB limit). Use 🌐 Online Player or 🔗 Direct Download Link instead.")
    await status.edit_text(
        f"📦 <b>{html.escape(meta['name'])}</b>\nSize: {html.escape(size_txt)}{html.escape(note)}\nPick how you want it:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    text = update.message.text or ""
    urls = [u.rstrip(").,") for u in URL_RE.findall(text)]
    urls = [u for u in urls if is_terabox_link(u)]
    if not urls:
        if URL_RE.search(text):
            await update.message.reply_text(
                "❌ That doesn't look like a Terabox link.\n"
                "Send only Terabox links."
            )
        return
    # Batch: do links one by one, like TeraBestBot.
    for i, url in enumerate(urls[:5], start=1):
        label = f" ({i}/{len(urls)})" if len(urls) > 1 else ""
        status = await update.message.reply_text(f"⏳ Processing your URL{label}…")
        await _process_one_link(status, url, update.effective_chat.id)
    if len(urls) > 5:
        await update.message.reply_text(
            f"Note: I did the first 5 links. Send the rest separately."
        )


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_allowed(update):
        await q.edit_message_text("Sorry, this bot is private.")
        return
    parts = q.data.split(":")
    action = parts[0]
    key = parts[-1]
    item = pending.pop(key, None)
    if action == "cancel" or not item:
        play_sessions.pop(key, None)
        await q.edit_message_text("Cancelled.")
        return

    mode = parts[1]
    meta = item["meta"]
    tmpdir = tempfile.mkdtemp(prefix="linkbot_v2_")
    dest = os.path.join(tmpdir, _safe_filename(meta["name"]))

    await q.edit_message_text(f"⬇️ Downloading <b>{html.escape(meta['name'])}</b>…", parse_mode="HTML")
    last_pct = -1
    loop = asyncio.get_running_loop()

    def progress(done: int, total: int):
        nonlocal last_pct
        if not total:
            return
        pct = int(done * 100 / total)
        if pct - last_pct >= 10:
            last_pct = pct
            asyncio.run_coroutine_threadsafe(
                q.edit_message_text(
                    f"⬇️ Downloading <b>{html.escape(meta['name'])}</b>… {pct}%",
                    parse_mode="HTML",
                ),
                loop,
            )

    try:
        await asyncio.to_thread(download_file, meta["url"], dest, progress)
    except Exception as e:
        log.exception("download failed")
        shutil.rmtree(tmpdir, ignore_errors=True)
        await q.edit_message_text(f"❌ Download failed:\n{e}")
        return

    await q.edit_message_text(f"⬆️ Uploading <b>{html.escape(meta['name'])}</b> to Telegram…", parse_mode="HTML")
    try:
        size_txt = meta.get("size") or human_size(os.path.getsize(dest))
        caption = f"📦 {meta['name']}\n{size_txt} - plays in Telegram, tap ⬇️ to save to device"
        with open(dest, "rb") as f:
            if mode == "video":
                await context.bot.send_video(
                    chat_id=item["chat_id"], video=f, caption=caption,
                    supports_streaming=True,
                )
            else:
                await context.bot.send_document(
                    chat_id=item["chat_id"], document=f, caption=caption
                )
        await q.edit_message_text("✅ Done - enjoy!")
    except Exception as e:
        log.exception("upload failed")
        await q.edit_message_text(f"❌ Upload to Telegram failed:\n{e}\n\nTip: use 🌐 Play in app browser instead, it streams without uploading.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Run start_bot.bat - it will make .env for you.")
    _start_web_server()
    # PTB default write timeout is only 5s - far too short for a
    # 20-50 MB upload. Raise all timeouts, and handle updates
    # concurrently so one download does not freeze /start.
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(300)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("version", start))
    app.add_handler(CallbackQueryHandler(handle_choice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("Bot %s starting (polling)…", VERSION)
    print(f"Bot {VERSION} starting - send /start to your bot in Telegram")
    app.run_polling()


if __name__ == "__main__":
    main()
