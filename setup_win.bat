@echo off
REM ============================================================================
REM GEO Audit Batch - Windows initial setup (run once)
REM ============================================================================

setlocal enableextensions
cd /d "%~dp0"

echo === [1/4] Creating Python venv ===
py -3 -m venv venv

echo === [2/4] Activating venv and upgrading pip ===
call venv\Scripts\activate.bat
python -m pip install --upgrade pip

echo === [3/4] Installing Python packages ===
pip install -r requirements.txt

echo === [4/4] Installing Playwright Chromium ===
python -m playwright install chromium

echo.
echo ============================================================================
echo Setup Complete! Next steps:
echo   1) Create .gcp folder and put your service account JSON key inside.
echo   2) Run run_batch.bat or run_jp.bat
echo ============================================================================
pause
