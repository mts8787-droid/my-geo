@echo off
REM ============================================================================
REM JP 전체 점검 (Full CSR, concurrency 5)
REM 다른 국가는 이 파일을 복사해서 reports\lg_urls_<country>.csv 로 바꿔 사용.
REM ============================================================================
cd /d "%~dp0"
call run_batch.bat reports\lg_urls_jp.csv --concurrency 5
