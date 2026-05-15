@echo off
chcp 65001 >nul
title GEO Audit 로컬 실행기
echo.
echo ========================================================
echo   GEO Audit 로컬 점검기를 준비 중입니다...
echo   (파이썬 스크립트를 실행합니다)
echo ========================================================
echo.

python local_runner.py

echo.
pause
