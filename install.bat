@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt || goto :err
python scripts\setup.py %* || goto :err
where npm >nul 2>nul && (cd web && call npm install --no-audit --no-fund && call npm run build && cd ..) || echo [!] Node.js not found: the dashboard bundle is only needed on the coordinator; build it there.
echo.
echo Done. Coordinator: start-coordinator.bat    Worker: start-worker.bat --coordinator ^<ip^> --code ^<code^>
exit /b 0
:err
echo Install failed.
exit /b 1
