@echo off
cd /d "%~dp0"
title Trading Bot IA
python app.py
if errorlevel 1 pause
