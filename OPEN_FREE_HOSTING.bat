@echo off
echo.
echo  Opening free always-on setup links...
echo  1) Your editable code on GitHub
echo  2) Railway (free trial / credits) - deploy from GitHub
echo  3) Full instructions
echo.
start "" "https://github.com/RemyCastle/peptide-inventory-bot"
timeout /t 1 /nobreak >nul
start "" "https://railway.app/new/github"
timeout /t 1 /nobreak >nul
start "" "https://github.com/RemyCastle/peptide-inventory-bot/blob/master/deploy/ALWAYS_ON.md"
echo.
echo  After deploy: STOP the local bot window so only cloud runs.
echo  Set env vars on the host: TELEGRAM_BOT_TOKEN, OWNER_IDS
echo.
pause
