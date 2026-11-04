@echo off
title Telegram Link Bot v2
cd /d "%~dp0"
echo ============================================
echo  Telegram Link Bot v2.0-clean
echo ============================================
echo.
python --version
if errorlevel 1 (
  echo Python is not installed or not in PATH.
  echo Install Python from python.org and tick "Add Python to PATH".
  pause
  exit /b 1
)
echo Installing what the bot needs (first time only, 1-2 min)...
pip install -r requirements.txt
if errorlevel 1 (
  echo Install failed. Copy the error above and send it to Muse.
  pause
  exit /b 1
)
if not exist .env (
  echo.
  echo Making .env for you - no rename needed...
  copy .env.example .env >nul
  echo Now Notepad will open. Fill in BOT_TOKEN and TERABOX_COOKIE, save, close.
  echo Do NOT share those values with anyone.
  notepad .env
)
echo.
echo Starting bot...
echo If you see "Bot v2.0-clean starting", it worked.
echo Keep this window open while you use the bot.
echo.
python bot.py
echo.
echo Bot stopped. Press any key to close.
pause >nul
