@echo off
REM 매매일지 웹앱 실행 — 브라우저에서 http://127.0.0.1:8000
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=py"
start "" http://127.0.0.1:8000
"%PY%" -m uvicorn webapp:app --host 127.0.0.1 --port 8000
