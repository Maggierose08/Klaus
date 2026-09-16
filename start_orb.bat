@echo off
cd /d "%~dp0"
py -3.12 orb.py

if errorlevel 1 (
    echo.
    echo Orb exited with an error. See above for details.
    pause
)
