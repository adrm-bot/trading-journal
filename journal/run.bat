@echo off
REM 매매일지 적재+알림 — 작업 스케줄러에 이 .bat 등록.
REM Windows Store 파이썬 스텁 회피: 실제 인터프리터 절대경로 사용.
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=py"
"%PY%" "%~dp0run_all.py"
