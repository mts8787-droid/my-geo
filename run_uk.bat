@echo off
REM ============================================================================
REM UK Full Audit
REM ============================================================================
cd /d "%~dp0"

echo [INFO] Starting UK Audit (Full CSR) and BigQuery Upload...
call run_batch.bat reports\lg_urls_uk.csv --concurrency 25

echo.
echo [INFO] UK Batch Process Completed.
