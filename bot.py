#!/usr/bin/env python3
"""
Telegram Link Bot — Terabox + Diskwala share links, served inside Telegram.

What the user gets for a Terabox video link:
  1. ▶️ Play here in Telegram — the bot downloads it server-side and
     re-uploads it as a Telegram video (supports_streaming=True), so it
     plays in the chat's own player. Nothing is saved to the device
     unless the user taps ⬇️ on that video.
  2. 🌐 Play in app browser — a button that opens a small player page
     hosted by this same bot (PUBLIC_URL/play/<key>). Telegram opens
     that link in its *in-app* browser, so the user never leaves
     Telegram, and the page streams the video with Range/seek support.
     This also works for files too big for Telegram to accept as an
     upload (>50 MB on the normal Bot API), because nothing is uploaded.
  3. ⬇️ Download / Save to device — the bot sends it as a document;
     the user taps ⬇️ only when they want it saved.

  Whenever possible the bot also offers the raw direct video link
  (the Terabox `dlink`) as a fallback button.

Setup: copy .env.example to .env and fill in BOT_TOKEN (from @BotFather),
TERABOX_COOKIE (see README), and PUBLIC_URL (your Render URL, after
the first deploy — needed for option 2 above).
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
from urllib.parse import parse_qs, urlparse

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

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TERABOX_COOKIE = os.getenv("TERABOX_COOKIE", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PLAY_TTL_HOURS = float(os.getenv("PLAY_TTL_HOURS", "6"))
MAX_FILE_BYTES = int(float(os.getenv("MAX_FILE_GB", "2")) * 1024**3)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("linkbot")

# ---------------------------------------------------------------- domains

TERABOX_DOMAINS = (
    "terabox.com", "teraboxapp.com", "1024terabox.com", "terabox.app",
    "teraboxshare.com", "mirrobox.com", "nephobox.com", "freemibox.com",
    "dubox.com",
)
DISKWALA_DOMAINS = ("diskwala", "diskswala")

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".ts", ".3gp"}

URL_RE = re.compile(r"https?://[^\s<>\"']+")

# resolved links waiting for the user to pick send-mode: key -> dict
pending: dict[str, dict] = {}
# in-app player sessions: key -> {"meta", "url", "service", "expires"}
play_sessions: dict[str, dict] = {}


def detect_service(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if any(d in host for d in TERABOX_DOMAINS):
        return "terabox"
    if any(d in host for d in DISKWALA_DOMAINS):
        return "diskwala"
    # Future-proof: Terabox keeps adding share domains (e.g. the
    # teraboxshare.com link that was rejected on 2026-11-04). Any host
    # with "terabox" in it is a Terabox link.
    if "terabox" in host:
        return "terabox"
    if "diskwala" in host:
        return "diskwala"
    return None


def human_size(n: int | None) -> str:
    if not n:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _is_video(meta: dict) -> bool:
    ext = os.path.splitext(meta.get("name", ""))[1].lower()
    return ext in VIDEO_EXTS or str(meta.get("mime", "")).startswith("video/")


def _terabox_headers() -> dict:
    """Headers Terabox's direct (dlink) URL needs — incl. the login cookie.

    The raw dlink often 403s in a plain browser without this cookie, which
    is why the bot proxies the stream itself instead of just handing the
    user the raw link.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.terabox.com/",
    }
    if TERABOX_COOKIE:
        headers["Cookie"] = TERABOX_COOKIE
    return headers


def _parse_size(value) -> tuple[str | None, int | None]:
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        return human_size(int(value)), int(value)
    s = str(value).strip()
    m = re.match(r"([\d.]+)\s*([KMGT]?B)", s, re.IGNORECASE)
    if m:
        num, unit = float(m.group(1)), m.group(2).upper()
        mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
        return s, int(num * mult)
    return s, None


# ------------------------------------------------------------ resolvers

def _find_between(s: str, start: str, end: str) -> str:
    i = s.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = s.find(end, i)
    return s[i:j] if j >= 0 else ""


def _resolve_terabox_custom(link: str) -> dict:
    """Custom Terabox resolver — no terabox-downloader, no forced Host header.

    Why this exists (2026-11-04): terabox-downloader 1.8 failed every time
    with "Failed to extract required tokens". Its page fetch forces
    `Host: www.terabox.app` even when the link redirects to a different
    Terabox host (e.g. www.1024tera.com), and it gives no clue which token
    was missing. This version follows redirects naturally, calls share/list
    on the host the page actually came from, and logs which tokens were
    found (booleans only — never the cookie itself).
    """
    if not TERABOX_COOKIE:
        raise RuntimeError("TERABOX_COOKIE is not set.")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/135.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.terabox.app/",
        "Cookie": TERABOX_COOKIE,
    }
    with requests.Session() as sess:
        # Short links like teraboxshare.com/s/<id> carry the surl in the
        # path itself, so go straight to the canonical share page instead
        # of following the short link's redirect chain (which can hang or
        # land on a different Terabox host each time).
        m_path = re.search(r"/s/([A-Za-z0-9_-]+)", urlparse(link).path)
        # Short links are /s/1<surl> — the leading "1" is not part of the
        # surl (verified: /s/1an6yIVbS9GGi8ABfBkaudQ redirects to
        # ?surl=an6yIVbS9GGi8ABfBkaudQ). Try the stripped surl first,
        # then the full path segment, then the original link itself.
        candidates = []
        if m_path:
            seg = m_path.group(1)
            candidates.append(
                f"https://www.terabox.app/sharing/link?surl={seg[1:]}"
            )
            candidates.append(
                f"https://www.terabox.app/sharing/link?surl={seg}"
            )
        candidates.append(link)
        r = None
        text = ""
        js_token = logid = bdstoken = ""
        surl = None
        last_err = None
        for cand in candidates:
            try:
                resp = sess.get(cand, headers=headers, timeout=20)
                if not resp.ok:
                    last_err = RuntimeError(
                        f"Terabox share page returned HTTP {resp.status_code}."
                    )
                    continue
            except Exception as e:  # noqa: BLE001 - try the next candidate
                last_err = e
                continue
            r, text = resp, resp.text
            final_url_tmp = resp.url
            qs_tmp = parse_qs(urlparse(final_url_tmp).query)
            surl = qs_tmp.get("surl", [None])[0] \
                or (m_path.group(1)[1:] if m_path else None)
            m_js = re.search(r"fn%28%22([0-9A-Fa-f]+)%22%29", text) \
                or re.search(r'jsToken":"([0-9A-Za-z]+)"', text)
            js_token = m_js.group(1) if m_js else ""
            m_log = re.search(r"dp-logid=(\d+)", text)
            logid = m_log.group(1) if m_log else ""
            m_bd = re.search(r'bdstoken":"([0-9a-fA-F]+)"', text)
            bdstoken = m_bd.group(1) if m_bd else ""
            if js_token and logid and bdstoken:
                break
            last_err = RuntimeError(
                "Terabox share page did not contain all login tokens."
            )
        if r is None or not r.ok:
            raise last_err or RuntimeError("Terabox share page could not be fetched.")
        final_url = r.url
        host = urlparse(final_url).netloc
        if not surl:
            raise RuntimeError("Terabox: could not find a share ID (surl) in the link.")
        log.info(
            "Terabox resolve: host=%s cookie_len=%d js=%s logid=%s bdstoken=%s",
            host, len(TERABOX_COOKIE), bool(js_token), bool(logid), bool(bdstoken),
        )
        if not js_token or not logid or not bdstoken:
            raise RuntimeError(
                "Terabox: could not read login tokens from the share page "
                f"(js={bool(js_token)}, logid={bool(logid)}, "
                f"bdstoken={bool(bdstoken)}, cookie_len={len(TERABOX_COOKIE)}). "
                "Terabox is not accepting your login cookie — re-copy the "
                "full ndus value while logged in on terabox.app, update "
                "TERABOX_COOKIE in Render, wait for Live, and try again."
            )
        params = {
            "app_id": "250528", "web": "1", "channel": "dubox",
            "clienttype": "0", "jsToken": js_token, "dp-logid": logid,
            "page": "1", "num": "20", "by": "name", "order": "asc",
            "site_referer": final_url, "shorturl": surl, "root": "1,",
        }
        r2 = sess.get(f"https://{host}/share/list", headers=headers,
                      params=params, timeout=30)
        data = r2.json()
        if not data or "list" not in data or not data["list"] or data.get("errno"):
            raise RuntimeError(
                f"Terabox share/list failed: {data.get('errmsg', data)}"
            )
        info = data["list"][0]
        size_bytes = int(info.get("size", 0) or 0)
        return {
            "name": info.get("server_filename") or "file",
            "size": human_size(size_bytes) if size_bytes else None,
            "size_bytes": size_bytes or None,
            "url": info.get("dlink"),
            "thumb": (info.get("thumbs") or {}).get("url3"),
        }


def resolve_terabox(link: str) -> dict:
    """Turn a Terabox share link into file metadata + a direct download URL."""
    try:
        return _resolve_terabox_custom(link)
    except Exception as custom_err:  # noqa: BLE001 - fall back to the library
        log.warning("Custom Terabox resolver failed: %s — trying terabox-downloader", custom_err)
        try:
            from TeraboxDL import TeraboxDL
        except ImportError:
            raise custom_err from None
        tb = TeraboxDL(TERABOX_COOKIE)
        info = tb.get_file_info(link)
        if isinstance(info, dict) and "error" in info:
            raise custom_err from None
        size_bytes = info.get("size_bytes")
        if size_bytes is not None:
            size_txt = info.get("file_size") or human_size(int(size_bytes))
        else:
            size_txt, size_bytes = _parse_size(info.get("file_size"))
        return {
            "name": info.get("file_name") or "file",
            "size": size_txt,
            "size_bytes": size_bytes,
            "url": info.get("download_link"),
            "thumb": info.get("thumbnail"),
        }


# ------------------------------------------------------------ Diskwala
#
# How Diskwala share links resolve (verified 2026-10-18 against a real link):
#   POST https://ddudapidd.diskwala.com/api/v1/file/temp_info
#   Body: {"id": "<24-hex share id from the /app/<id> URL>"}
#   -> JSON with fileInfo {name, type, size, url}; `url` is the download URL.
#
# The endpoint is gated by an `Appicrypt` header (Talsec app-integrity SDK):
# the token is generated at runtime by their JavaScript and is bound to a
# fresh nonce + timestamp, so it cannot be reused or forged statically.
# The reliable approach is to let the genuine page JS generate it inside a
# headless browser and intercept the API response (resolve_diskwala()).

DISKWALA_API = "https://ddudapidd.diskwala.com/api/v1/file/temp_info"


def _diskwala_share_id(link: str) -> str:
    m = re.search(r"/app/([0-9a-fA-F]+)", link)
    if not m:
        raise ValueError("Could not find a Diskwala share ID in the link.")
    return m.group(1)


def _diskwala_meta_from_fileinfo(info: dict) -> dict:
    size_txt, size_bytes = _parse_size(info.get("size"))
    url = info.get("url")
    if not url:
        raise RuntimeError("Diskwala response had no download URL (fileInfo.url).")
    return {
        "name": info.get("name") or "file",
        "size": size_txt,
        "size_bytes": size_bytes,
        "url": url,
        "mime": info.get("type"),
        "thumb": info.get("thumbnail"),
    }


def resolve_diskwala(link: str) -> dict:
    """Resolve a Diskwala share link via a headless browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            "  pip install playwright && playwright install chromium"
        ) from e

    captured: dict = {}

    def on_response(resp):
        if "temp_info" in resp.url:
            try:
                captured["json"] = resp.json()
            except Exception:  # noqa: BLE001
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"
        )
        page.on("response", on_response)
        page.goto(link, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        browser.close()

    data = captured.get("json")
    if not data:
        raise RuntimeError(
            "Diskwala page loaded but no file-info response was captured. "
            "The link may be expired/removed, or the site changed its flow."
        )
    info = data.get("fileInfo") or (data.get("data") or {}).get("fileInfo") or {}
    if not info:
        raise RuntimeError(f"Unexpected Diskwala response shape: {str(data)[:200]}")
    return _diskwala_meta_from_fileinfo(info)


# ------------------------------------------------------------ download

def download_file(url: str, dest: str, progress_cb=None, service: str | None = None) -> int:
    """Stream-download url to dest. Returns total bytes written."""
    headers = _terabox_headers() if service == "terabox" else {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    with requests.get(url, stream=True, timeout=60, headers=headers) as r:
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


# ------------------------------------------------------------ in-app player
# The same web server Render health-checks also serves:
#   GET /play/<key>      -> a tiny <video> page (opens in Telegram's
#                           in-app browser, so the user never leaves Telegram)
#   GET /stream/<key>    -> the video itself, proxied from Terabox with
#                           Range support so seeking works
#   GET /download/<key>  -> same stream, as an attachment (save to device)

def _player_html(meta: dict, key: str) -> str:
    name = html.escape(meta.get("name") or "video")
    size = html.escape(meta.get("size") or "")
    thumb = html.escape(meta.get("thumb") or "")
    poster = f' poster="{thumb}"' if thumb else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
body {{ margin: 0; background: #111; color: #eee; font-family: sans-serif; }}
.wrap {{ max-width: 720px; margin: 0 auto; padding: 16px; }}
video {{ width: 100%; background: #000; border-radius: 8px; }}
a.btn {{ display: block; text-align: center; margin-top: 14px; padding: 12px;
  background: #2ea6ff; color: #fff; text-decoration: none; border-radius: 8px; }}
p {{ color: #aaa; font-size: 14px; }}
</style></head>
<body><div class="wrap">
<h2>{name}</h2><p>{size}</p>
<video src="/stream/{key}" controls autoplay playsinline preload="metadata"{poster}></video>
<a class="btn" href="/download/{key}">⬇️ Download / Save to device</a>
<p>Playing inside Telegram's in-app browser. If the video doesn't start,
the Terabox link may have expired — send the link to the bot again.</p>
</div></body></html>"""


class _WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence it
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/play/"):
            self._serve_player(path[len("/play/"):])
            return
        if path.startswith("/stream/"):
            self._serve_stream(path[len("/stream/"):], download=False)
            return
        if path.startswith("/download/"):
            self._serve_stream(path[len("/download/"):], download=True)
            return
        self.send_response(404)
        self.end_headers()

    def _lookup(self, key: str):
        sess = play_sessions.get(key)
        if not sess:
            return None
        if sess["expires"] < time.time():
            play_sessions.pop(key, None)
            return None
        return sess

    def _serve_player(self, key: str):
        sess = self._lookup(key)
        if not sess:
            body = b"Link expired or unknown. Send the Terabox link to the bot again."
            self.send_response(410)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = _player_html(sess["meta"], key).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self, key: str, download: bool):
        sess = self._lookup(key)
        if not sess or not sess.get("url"):
            self.send_response(410)
            self.end_headers()
            return
        meta = sess["meta"]
        headers = _terabox_headers() if sess.get("service") == "terabox" else {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        range_header = self.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        try:
            r = requests.get(sess["url"], stream=True, timeout=30, headers=headers)
        except Exception:  # noqa: BLE001
            log.exception("player stream failed")
            self.send_response(502)
            self.end_headers()
            return
        ctype = r.headers.get("content-type") or mimetypes.guess_type(meta.get("name", ""))[0] \
            or ("video/mp4" if _is_video(meta) else "application/octet-stream")
        self.send_response(r.status_code if r.status_code in (200, 206) else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if r.headers.get("content-length"):
            self.send_header("Content-Length", r.headers["content-length"])
        if r.headers.get("content-range"):
            self.send_header("Content-Range", r.headers["content-range"])
        if download:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{meta.get("name", "file")}"'
            )
        else:
            self.send_header("Content-Disposition", "inline")
        self.end_headers()
        try:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            r.close()


def _start_web_server():
    port = os.getenv("PORT")
    if not port:
        return
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), _WebHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Web server (health + in-app player) on port %s", port)


# ------------------------------------------------------------ handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send me a Terabox or Diskwala share link.\n\n"
        "I'll give you 3 choices — all inside Telegram:\n"
        "▶️ Play here — plays in the chat\n"
        "🌐 Play in app browser — opens in Telegram's in-app browser\n"
        "⬇️ Download — only saves to your device if you tap it"
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    m = URL_RE.search(text)
    if not m:
        return
    url = m.group(0).rstrip(").,")
    service = detect_service(url)
    if not service:
        await update.message.reply_text(
            "❌ That doesn't look like a Terabox or Diskwala link."
        )
        return

    status = await update.message.reply_text("🔍 Resolving link…")
    try:
        if service == "terabox":
            if not TERABOX_COOKIE:
                await status.edit_text(
                    "⚠️ TERABOX_COOKIE is not set in .env.\n"
                    "Many Terabox links need a login cookie to resolve — "
                    "see README.md for how to grab yours."
                )
                return
            meta = await asyncio.to_thread(resolve_terabox, url)
        else:
            meta = await asyncio.to_thread(resolve_diskwala, url)
    except Exception as e:  # noqa: BLE001 - show resolver errors to the user
        log.exception("resolve failed")
        await status.edit_text(f"❌ Couldn't resolve that link:\n{e}")
        return

    if not meta.get("url"):
        await status.edit_text("❌ Resolved the link but got no download URL.")
        return

    size_bytes = meta.get("size_bytes")
    too_big = bool(size_bytes and size_bytes > MAX_FILE_BYTES)
    is_video = _is_video(meta)

    key = uuid.uuid4().hex[:8]
    pending[key] = {"meta": meta, "chat_id": update.effective_chat.id, "service": service}
    play_sessions[key] = {
        "meta": meta, "url": meta["url"], "service": service,
        "expires": time.time() + PLAY_TTL_HOURS * 3600,
    }

    buttons: list[list[InlineKeyboardButton]] = []
    if is_video:
        if not too_big:
            buttons.append([InlineKeyboardButton(
                "▶️ Play here in Telegram", callback_data=f"send:video:{key}")])
        if PUBLIC_URL:
            buttons.append([InlineKeyboardButton(
                "🌐 Play in app browser", url=f"{PUBLIC_URL}/play/{key}")])
        elif meta.get("url"):
            # No PUBLIC_URL yet: fall back to the raw direct link. Telegram
            # still opens it in its in-app browser, but it may expire soon
            # and may need the Terabox cookie, so the player above is better.
            buttons.append([InlineKeyboardButton(
                "🌐 Play in app browser (direct link)", url=meta["url"])])
        if not too_big:
            buttons.append([InlineKeyboardButton(
                "⬇️ Download / Save to device", callback_data=f"send:file:{key}")])
        elif meta.get("url"):
            buttons.append([InlineKeyboardButton(
                "🔗 Direct video link", url=meta["url"])])
    else:
        if not too_big:
            buttons.append([InlineKeyboardButton(
                "⬇️ Download / Save to device", callback_data=f"send:file:{key}")])
        if PUBLIC_URL:
            buttons.append([InlineKeyboardButton(
                "🌐 Open in app browser", url=f"{PUBLIC_URL}/play/{key}")])
        elif meta.get("url"):
            buttons.append([InlineKeyboardButton(
                "🔗 Direct link", url=meta["url"])])
    buttons.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{key}")])

    size_txt = meta.get("size") or human_size(size_bytes)
    note = ""
    if too_big:
        note = (f"\n⚠️ This file is {size_txt} — too big to send through "
                "Telegram as a file, so use 🌐 Play in app browser instead. "
                "It streams without a full download.\n")
    if is_video and PUBLIC_URL:
        note += f"\n🌐 Play link works for {PLAY_TTL_HOURS:g} hours, inside Telegram."
    elif is_video and not PUBLIC_URL:
        note += ("\nℹ️ Set PUBLIC_URL after deploying (see README) and the "
                 "🌐 button will open the bot's own player instead of the raw link.")
    await status.edit_text(
        f"📦 **{meta['name']}**\n"
        f"Size: {size_txt}\n"
        f"{note}\n"
        "Pick how you want it:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, *rest = q.data.split(":")
    key = rest[-1]
    item = pending.pop(key, None)
    if action == "cancel" or not item:
        play_sessions.pop(key, None)
        await q.edit_message_text("Cancelled.")
        return

    mode = rest[0]  # video | file
    meta = item["meta"]
    tmpdir = tempfile.mkdtemp(prefix="linkbot_")
    dest = os.path.join(tmpdir, meta["name"])

    await q.edit_message_text(f"⬇️ Downloading **{meta['name']}**…", parse_mode="Markdown")
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
                    f"⬇️ Downloading **{meta['name']}**… {pct}%",
                    parse_mode="Markdown",
                ),
                loop,
            )

    try:
        await asyncio.to_thread(
            download_file, meta["url"], dest, progress, item.get("service")
        )
    except Exception as e:  # noqa: BLE001
        log.exception("download failed")
        shutil.rmtree(tmpdir, ignore_errors=True)
        await q.edit_message_text(f"❌ Download failed:\n{e}")
        return

    await q.edit_message_text(f"⬆️ Uploading **{meta['name']}** to Telegram…", parse_mode="Markdown")
    try:
        size_txt = meta.get("size") or human_size(os.path.getsize(dest))
        caption = f"📦 {meta['name']}\n{size_txt} — plays in Telegram, tap ⬇️ to save to device"
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
        await q.edit_message_text("✅ Done — enjoy!")
    except Exception as e:  # noqa: BLE001
        log.exception("upload failed")
        await q.edit_message_text(
            "❌ Upload to Telegram failed:\n"
            f"{e}\n\nTip: files over 50 MB need a local Bot API server — see README.md. "
            "Or use the 🌐 Play in app browser button instead, it streams without uploading."
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    _start_web_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_choice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("Bot starting (polling)…")
    app.run_polling()


if __name__ == "__main__":
    main()
