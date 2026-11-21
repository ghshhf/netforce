@echo off
rem 相对脚本自身定位，换电脑 clone 后无需改路径。
rem 真正逻辑交给 PowerShell：无窗口、跨账户、运行时发现 Python，零硬编码盘符。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_launch.ps1" "%~dp0"
