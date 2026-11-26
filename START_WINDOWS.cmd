@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul || (echo Install Python 3.11+ with the Windows py launcher. & pause & exit /b 1)
if not exist ".venv\Scripts\python.exe" (py -3 -m venv .venv || goto :failed)
".venv\Scripts\python.exe" -c "import openpyxl, xlsxwriter" >nul 2>nul || (".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :failed)
".venv\Scripts\python.exe" scripts\cost.py || goto :failed
echo Open http://127.0.0.1:8080 in your browser. Keep this window open.
".venv\Scripts\python.exe" app.py serve
if errorlevel 1 goto :failed
exit /b 0
:failed
echo Startup failed. See README.md for manual setup.
pause
exit /b 1
