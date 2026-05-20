 @echo off
echo.
echo ========================================================
echo   Auto-installing required packages and browsers...
echo   (This may take a moment if it's the first run)
echo ========================================================
python -m pip install -r requirements.txt
python -m playwright install chromium
echo.

set "URLS=https://www.lg.com/uk/support/product-support/cs-OLED83M39LA.AVS/,https://www.lg.com/uk/support/product-support/troubleshoot/help-library/cs-CT00008386-20154860355050/,https://www.lg.com/uk/support/product-support/cs-43UH620V.AEK/,https://www.lg.com/uk/support/product-support/troubleshoot/help-library/cs-CT00008333-20154858260175/,https://www.lg.com/uk/support/product-support/cs-OLED55C45LA.AEK/,https://www.lg.com/uk/support/product-support/cs-86UH5N-M.AEK/,https://www.lg.com/uk/support/product-support/cs-GMX844MCKV.AMCQLGU/,https://www.lg.com/uk/fridge-freezers/tall-fridge-freezers/gbm21hsadh/,https://www.lg.com/uk/business/information-display/digital-signage/video-wall/55vl7f-a/,https://www.lg.com/uk/tvs-soundbars/oled/oled55b46la/"

venv\Scripts\python.exe local_runner.py --urls "%URLS%" --name "UK_Quick_10"
pause
