@echo off
cd /d "%~dp0"
rem Already running? Just open the browser (with version tag to bust cache), no double start
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if(-not $c){ exit 1 }"
if %errorlevel%==0 (
    start "" "http://127.0.0.1:8765/?v=smile3"
    exit /b
)
rem Start hidden with pythonw (fall back to python)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" /min pythonw web_ui.py --port 8765
) else (
    start "" /min python web_ui.py --port 8765
)
rem Wait for the service then open browser (max 12s)
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 12;$i++){ Start-Sleep -Milliseconds 900; $c=Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if($c){$ok=$true; break} }; if($ok){ Start-Process 'http://127.0.0.1:8765/?v=smile3' } else { Write-Host 'SERVICE FAILED - run manually: python web_ui.py' -ForegroundColor Red; Start-Sleep 3 }"
exit /b
