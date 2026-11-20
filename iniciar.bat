@echo off
chcp 65001 >nul
title Stream Deck do Celular
cd /d "%~dp0"

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
  echo.
  echo Python nao encontrado neste PC.
  echo Instale em https://www.python.org/downloads/ e marque a opcao
  echo "Add python.exe to PATH" na primeira tela do instalador.
  echo.
  start https://www.python.org/downloads/
  pause
  exit /b 1
)

%PY% server.py
echo.
echo O servidor foi encerrado. Se foi um erro, veja o arquivo streamdeck.log
pause
