@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Run setup.bat first.
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m campusvoice.diagnostics --key-status --model-status --devices --live-check
exit /b %errorlevel%
