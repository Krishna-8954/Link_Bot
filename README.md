# Telegram Link Bot — Terabox + Diskwala → Telegram files

Send the bot a Terabox or Diskwala share link. You get 3 choices, all
inside Telegram — nothing opens an outside app:

1. **▶️ Play here in Telegram** — the bot downloads the file server-side
   and re-uploads it as a Telegram video, so it plays in the chat's own
   player. It only saves to your device if you tap ⬇️ on that video.
2. **🌐 Play in app browser** — opens a small player page hosted by the
   bot itself (`/play/<key>`). Telegram opens it in its **in-app browser**,
   so you never leave Telegram, and the video streams with seek support.
   This is the option that works for big files too (>50 MB), because
   nothing is uploaded to Telegram — the bot just proxies the stream.
   The play link lasts 6 hours by default (`PLAY_TTL_HOURS`).
3. **⬇️ Download / Save to device** — the bot sends it as a document;
   tap ⬇️ only when you want it saved.

Whenever possible the bot also offers the raw direct video link
(Terabox `dlink`) as a fallback. Note: that raw link often needs the
Terabox login cookie and expires quickly, which is why the bot's own
player (option 2) is the reliable one — the server sends the cookie
for you when it streams.

## 1. Create the bot (2 min)

1. Open Telegram, message **@BotFather**.
2. Send `/newbot`, follow the prompts, copy the **token** it gives you.
3. (Optional) send `/setdescription` to give it a nice bio.

## 2. Run it locally (test)

```bash
cd telegram-link-bot
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and paste your BOT_TOKEN
python bot.py
```

Send `/start` to your bot in Telegram, then paste a Terabox link.

## 3. Terabox cookie (needed for most links)

Public Terabox links usually refuse to resolve without a logged-in session.
The bot reads it from `TERABOX_COOKIE` in `.env`:

1. In Chrome, log in at terabox.com.
2. Press **F12 → Application tab → Cookies → https://www.terabox.com**.
3. Copy the value of the `ndus` cookie (this is your login session).
4. In `.env`, set e.g.:
   `TERABOX_COOKIE=lang=en; ndus=Yxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
5. Restart the bot.

Cookies expire (you'll get resolve errors when they do) — just repeat the
steps and update `.env`.

## 4. Diskwala resolver (already wired — here's how it works)

Diskwala publishes no API. Their share page (`diskwala.com/app/<id>`) is a
JavaScript app that resolves the link with a single call:

- `POST https://ddudapidd.diskwala.com/api/v1/file/temp_info`
- Body: `{"id": "<the ID from the share URL>"}`
- Response JSON contains `fileInfo` = `{name, type, size, url}`, where `url`
  is the real download/stream URL. Works anonymously, no login.

The catch: every call must carry an `Appicrypt` header — a Talsec
app-integrity token generated at runtime by their page JavaScript (bound to
a fresh nonce + timestamp, so it can't be copied or reused). The server
rejects requests without it (`400 Missing Appicrypt header`).

So `resolve_diskwala()` in `bot.py` doesn't forge the token — it loads the
share page in a **headless Chromium** (Playwright), lets the genuine page JS
generate a valid token, and intercepts the `temp_info` response to grab the
download URL. `resolve_diskwala_direct()` is also in the file as a reference
implementation of the raw HTTP call.

Setup for the Diskwala path:

```bash
pip install playwright
playwright install chromium
```

Notes:
- Headless Chromium adds ~170 MB at install and uses ~200-400 MB RAM per
  resolve. Fine on a VPS; on Render's free tier it works but makes deploys
  slower and cold starts longer.
- If Chromium fails to launch on Render with "missing dependencies" errors,
  the native runtime can't install OS packages — the Diskwala path then
  needs Render's Docker runtime or a VPS (Option A) instead.
- If Diskwala changes their page flow, the resolver raises a clear error
  naming the `temp_info` capture — re-check the page's network calls.
- Their page shows **no download button** at all (only app-install CTAs),
  so this headless approach is currently the only server-side way in.

## 5. Deploy — pick one

### Option A: Oracle Cloud Always Free (recommended, always awake)

1. Sign up at cloud.oracle.com, create an **Ampere ARM** VM (4 cores /
   24 GB RAM free) with Ubuntu.
2. SSH in and set up:
   ```bash
   sudo apt update && sudo apt install -y python3-venv
   # copy this project to /opt/linkbot, e.g. via scp or git
   cd /opt/linkbot && python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   cp .env.example .env && nano .env   # fill in BOT_TOKEN etc.
   sudo cp linkbot.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now linkbot
   journalctl -u linkbot -f   # watch logs
   ```

### Option B: Render free tier (quick test only)

1. Push this folder to a GitHub repo.
2. On render.com → New → Web Service → connect the repo. `render.yaml`
   fills in the build/start commands; add `BOT_TOKEN` (and
   `TERABOX_COOKIE`) as environment variables.
3. Free tier sleeps after ~15 min idle and wipes temp disk on restart —
   fine for testing, not for a real bot.
4. **After the first deploy, set PUBLIC_URL.** Render shows your service
   URL (like `https://telegram-link-bot-xxxx.onrender.com`). In Render →
   your service → Environment, add `PUBLIC_URL` with that exact URL
   (no trailing `/`), then redeploy / restart. Without this, the 🌐
   button falls back to the raw Terabox link instead of the bot's
   own in-app player.

## 6. Files bigger than 50 MB

The default Telegram Bot API caps **bot uploads at 50 MB**. To send files
up to 2 GB, run a local Bot API server on your VPS and point the bot at it
(see https://core.telegram.org/bots/api#using-a-local-bot-api-server).
With `python-telegram-bot`:

```python
app = Application.builder().token(BOT_TOKEN).base_url("http://localhost:8081/bot").build()
```

(Replace the `Application.builder()` line in `bot.py`.) The bot still needs
enough disk/RAM for the temp download.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Couldn't resolve that link" on Terabox | Cookie missing/expired → redo step 3 |
| Download stalls on big files | Host bandwidth/disk; check `df -h` on the VPS |
| Upload fails over 50 MB | Need local Bot API server (step 6) |
| Bot silent on Render after a while | Free tier slept; wake it by messaging, or move to Oracle |
| Diskwala links fail ("no file-info response was captured") | Link expired/removed, or Diskwala changed its page flow — re-check the share page's network calls (see step 4) |

## Notes

- Every file passes through your server: budget bandwidth + temp disk.
- Only use this with files you have the right to handle.
- Never commit your real `.env` (with the token) to git.
