@echo off
chcp 65001 >nul
cd /d "%~dp0CustomApiTranslator"
python mtool_custom_api.py
pause
