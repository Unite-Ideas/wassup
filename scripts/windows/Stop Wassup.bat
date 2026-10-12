@echo off
title Stop Wassup
rem Stops Wassup cleanly inside Ubuntu (WSL). Your data is kept.
wsl.exe -e bash -lc "bash ~/wassup/scripts/stop.sh"
echo.
pause
