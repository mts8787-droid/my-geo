@echo off
REM ============================================================================
REM GEO Audit Batch - Windows generic runner
REM ============================================================================

setlocal enableextensions
cd /d "%~dp0"

set "GOOGLE_APPLICATION_CREDENTIALS="
for %%f in (".gcp\*.json") do set "GOOGLE_APPLICATION_CREDENTIALS=%%~ff"

set "BQ_PROJECT=geo-dashboad-raw"
set "BQ_DATASET=lg_geo_audit"
set "BQ_TABLE=audit_results"
set "BQ_LOCATION=US"

if not defined GOOGLE_APPLICATION_CREDENTIALS (
    echo.
    echo [ERROR] Service account key not found.
    echo Please create a .gcp folder and put your JSON key inside.
    echo.
    pause
    exit /b 1
)

if not exist "venv\Scripts\activate.bat" (
    echo.
    echo [ERROR] venv not found. Please run setup_win.bat first.
    echo.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo.
    echo Usage: %~nx0 ^<csv-path^> [options...]
    echo Example: %~nx0 reports\lg_urls_jp.csv
    echo.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo.
echo ============================================================================
echo  GEO Audit Batch
echo ============================================================================
echo  CSV         : %1
echo  Credentials : Auto-detected from .gcp folder
echo ============================================================================
echo.

python batch_audit.py %*

set "EXITCODE=%ERRORLEVEL%"
echo.
echo ============================================================================
echo  Done. exit code = %EXITCODE%
echo ============================================================================
pause
exit /b %EXITCODE%
