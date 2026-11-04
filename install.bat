@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)
echo Installing dependencies ...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Install failed. Check your network and try again.
  pause
  exit /b 1
)
echo.
echo Done. Double-click "start-web.bat" to open the app in your browser.
pause
