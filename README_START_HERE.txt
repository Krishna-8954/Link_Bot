TELEGRAM LINK BOT v3 - LIKE @TeraBestBot
==========================================
Inspected @TeraBestBot (t.me/terabestbot):
  "TeraBot Link To Video Bot | Tera Box DL"
  ~58k monthly users. Send link -> Processing -> Online Player + Direct Link.
  Handles 10GB+ files, folder & batch links, no login required (claimed).

v3 copies that flow:
  - 🌐 Online Player FIRST, 🔗 Direct Download Link SECOND
  - Telegram upload only for small files (<50 MB) - big files use link/player, never upload
  - Folder links list ALL files (up to 8 shown per message)
  - Batch: send up to 5 links in one message, bot does them one by one
  - Tries guest / no-login first - TERABOX_COOKIE is now OPTIONAL in .env
    Fill it only if a link fails with empty dlink.
  - /start and /help are simple, like TeraBestBot
  - All v2.1 fixes kept: timeouts, concurrent_updates, HTML escaping,
    Windows-safe names, 8080 player when PUBLIC_URL set, RFC5987 Hindi names,
    403 -> real 502, ALLOWED_USER_IDS (blank = allow everyone)

STEPS - ONE AT A TIME:
  Step 1 - Download this zip, unzip on Desktop, folder telegram-link-bot-v3
  Step 2 - Double-click start_bot.bat - it installs, then opens Notepad with .env
  Step 3 - In Notepad fill BOT_TOKEN only. Leave TERABOX_COOKIE blank to try
           guest mode first. Leave PUBLIC_URL blank on PC. Save, close.
  Step 4 - Bot starts. In Telegram send /start, then a Terabox link.
           Tap 🌐 Online Player to watch, 🔗 Direct Download Link to download.

HONEST NOTE: Not tested live against Terabox/Telegram yet.
TeraBestBot runs on its own servers with its own Terabox session.
Your PC bot may still need the cookie for some links if Terabox
gives an empty dlink to a guest. If that happens, add the cookie and retry.
