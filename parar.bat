@echo off
title Parar Stream Deck
rem Encerra o servidor iniciado por iniciar_sem_janela.vbs
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*server.py*' -and $_.Name -match '^pythonw?\.exe$' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Stream Deck encerrado.
timeout /t 2 >nul
