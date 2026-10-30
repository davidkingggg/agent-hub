@echo off
rem Double-click to restart the Agent Hub remote control (minimized console keeps it alive).
rem Stop it with: python launch.py --stop   (or just close the console window)
cd /d "%~dp0"
title Agent Hub remote
"python" launch.py
if errorlevel 1 pause
