@echo off
REM ============================================================================
REM JP Full Audit (Full CSR, concurrency 5)
REM ============================================================================
cd /d "%~dp0"
call run_batch.bat reports\lg_urls_jp.csv --concurrency 5
