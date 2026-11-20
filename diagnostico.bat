@echo off
chcp 65001 >nul
title Diagnostico do Stream Deck
cd /d "%~dp0"
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python nao encontrado. Instale em https://www.python.org/downloads/
  pause
  exit /b 1
)
%PY% server.py --diag
echo.
pause
