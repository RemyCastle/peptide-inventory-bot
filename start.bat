@echo off
cd /d "%~dp0"
if not exist venv (
  echo Creating virtual environment...
  python -m venv venv
)
call venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
if not exist .env (
  echo.
  echo No .env found. Copying .env.example to .env
  copy .env.example .env >nul
  echo Edit .env and set TELEGRAM_BOT_TOKEN and OWNER_IDS, then run start.bat again.
  notepad .env
  pause
  exit /b 1
)
echo Starting peptide inventory bot...
python bot.py
pause
