@echo off
REM ============================================================================
REM GEO Audit Batch - Windows initial setup (run once)
REM ============================================================================
REM 사전 요구사항:
REM   - Python 3.9+  (https://www.python.org/downloads/  설치 시 "Add to PATH" 체크)
REM   - 이 폴더에 .py 소스, requirements.txt, batch_audit.py가 있어야 함
REM   - 서비스 계정 JSON 키를 %USERPROFILE%\.gcp\geo-audit-batch.json 로 복사
REM ============================================================================

setlocal enableextensions
cd /d "%~dp0"

echo === [1/4] Python venv 생성 ===
py -3 -m venv venv
if errorlevel 1 (
    echo.
    echo [ERROR] Python 3가 없거나 'py' 명령이 동작하지 않습니다.
    echo https://www.python.org/downloads/ 에서 설치 + "Add Python to PATH" 체크
    pause
    exit /b 1
)

echo === [2/4] venv 활성화 + pip 업그레이드 ===
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] pip 업그레이드 실패
    pause
    exit /b 1
)

echo === [3/4] Python 패키지 설치 (requirements.txt) ===
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install 실패. 네트워크/프록시 확인.
    pause
    exit /b 1
)

echo === [4/4] Playwright Chromium 다운로드 ===
python -m playwright install chromium
if errorlevel 1 (
    echo [WARN] Playwright Chromium 설치 실패. CSR 분석 비활성됨.
    echo --lightweight 옵션으로는 계속 사용 가능.
)

echo.
echo ============================================================================
echo Setup 완료. 다음 단계:
echo.
echo   1) %USERPROFILE%\.gcp\ 폴더 생성 후, 서비스 계정 키 파일을 그 안에 복사
echo      예) %USERPROFILE%\.gcp\geo-audit-batch.json
echo.
echo   2) run_batch.bat 또는 run_jp.bat 실행
echo ============================================================================
pause
