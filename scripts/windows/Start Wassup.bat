@echo off
title Start Wassup
rem Starts Wassup inside Ubuntu (WSL). Open Docker Desktop and Ollama first.
wsl.exe -e bash -lc "bash ~/wassup/scripts/start.sh"
echo.
pause
