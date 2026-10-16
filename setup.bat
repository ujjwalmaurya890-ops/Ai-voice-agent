@echo off
setlocal
cd /d "%~dp0"

set PY=py -3
where py >nul 2>nul
if errorlevel 1 set PY=python

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  echo Install it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH".
  exit /b 1
)

if not exist .venv\Scripts\activate.bat (
  %PY% -m venv .venv
  if errorlevel 1 exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1

echo.
echo Setup complete. Next: test.bat, then check.bat, then run.bat
