@echo off
rem 相对脚本自身定位，换电脑 clone 后无需改路径
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
rem idempotent guard: exit if resource_finder already running (prevents double instance)
powershell -NoProfile -Command "if(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object {$_.CommandLine -like '*resource_finder*'}){exit 1}else{exit 0}"
if errorlevel 1 exit /b 0
python resource_finder.py >> "%~dp0_crawler.log" 2>&1
