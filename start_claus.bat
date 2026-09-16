@echo off
cd /d "%~dp0"
py -3.12 claus.py

if errorlevel 1 (
    echo.
    echo Claus exited with an error. See above for details.
    pause
)
