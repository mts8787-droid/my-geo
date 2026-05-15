@echo off
REM ============================================================================
REM GEO Audit Batch - Windows generic runner
REM ============================================================================
REM 사용법:
REM   run_batch.bat reports\lg_urls_jp.csv
REM   run_batch.bat reports\lg_urls_jp.csv --limit 100 --concurrency 5
REM   run_batch.bat reports\lg_urls_jp.csv --lightweight --concurrency 30
REM ============================================================================

setlocal enableextensions
cd /d "%~dp0"

REM === BigQuery 환경변수 ===
REM 키 파일은 프로젝트 루트의 .gcp\ 폴더 안의 첫 .json 파일을 자동 사용.
REM (파일명 그대로 유지 가능 — 폐기/재발급 시 파일만 교체)
set "GOOGLE_APPLICATION_CREDENTIALS="
for %%f in (".gcp\*.json") do set "GOOGLE_APPLICATION_CREDENTIALS=%%~ff"

set "BQ_PROJECT=geo-dashboad-raw"
set "BQ_DATASET=lg_geo_audit"
set "BQ_TABLE=audit_results"
set "BQ_LOCATION=US"

REM === User-Agent ===
REM 회사 IP가 LG Akamai 화이트리스트에 등록되어 있다면 아래 줄을 그대로 두세요
REM (기본 MyGEOAudit UA 사용).
REM 만약 회사 IP에서도 403이 뜨면 아래 REM을 떼서 Chrome UA로 우회하세요.
REM set "AUDIT_USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

REM === 키 파일 존재 확인 ===
if not defined GOOGLE_APPLICATION_CREDENTIALS (
    echo.
    echo [ERROR] 서비스 계정 키를 찾을 수 없습니다.
    echo 프로젝트 루트(%~dp0)에 .gcp\ 폴더를 만들고 서비스 계정 JSON 키를
    echo 그 안에 넣어주세요. 파일명은 변경하지 않아도 됩니다.
    echo.
    pause
    exit /b 1
)

REM === venv 존재 확인 ===
if not exist "venv\Scripts\activate.bat" (
    echo.
    echo [ERROR] venv가 없습니다. 먼저 setup_win.bat을 실행하세요.
    echo.
    pause
    exit /b 1
)

REM === 인자 검사 ===
if "%~1"=="" (
    echo.
    echo 사용법: %~nx0 ^<csv-path^> [options...]
    echo.
    echo 예시:
    echo   %~nx0 reports\lg_urls_jp.csv
    echo   %~nx0 reports\lg_urls_jp.csv --limit 100 --concurrency 5
    echo   %~nx0 reports\lg_urls_jp.csv --lightweight --concurrency 30
    echo.
    echo BigQuery 적재 생략하려면 --no-upload, 적재만 따로 하려면 --only-upload
    echo.
    pause
    exit /b 1
)

REM === venv 활성화 + 실행 ===
call venv\Scripts\activate.bat

echo.
echo ============================================================================
echo  GEO Audit Batch
echo ============================================================================
echo  CSV         : %1
echo  Project     : %BQ_PROJECT%
echo  Dataset     : %BQ_DATASET%.%BQ_TABLE%  (%BQ_LOCATION%)
echo  Credentials : %GOOGLE_APPLICATION_CREDENTIALS%
echo                ^(auto-detected from .gcp\^)
if defined AUDIT_USER_AGENT (
    echo  User-Agent  : Chrome UA override
) else (
    echo  User-Agent  : MyGEOAudit ^(default - assumes whitelisted IP^)
)
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
