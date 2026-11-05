@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [ERROR] Virtual environment belum ada.
  echo Jalankan dulu:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
)

echo.
echo   EarnApp Monitor
echo   Buka: http://127.0.0.1:8080
echo   Tutup jendela ini atau tekan Ctrl+C untuk berhenti.
echo.

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8080'"

".venv\Scripts\python.exe" run.py
pause
