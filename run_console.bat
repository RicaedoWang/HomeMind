@echo off
rem HomeMind Web 控制台一键启动（双击即可）
rem 用 homemind conda 环境的 python；脚本自身会把 src 挂进 sys.path
chcp 65001 >nul
cd /d %~dp0
D:\anaconda\envs\homemind\python.exe scripts\console_server.py
pause
