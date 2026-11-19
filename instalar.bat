@echo off
chcp 65001 >nul
title Instalar DROPIA Clips
cd /d "%~dp0"
echo.
echo  Instalando DROPIA Clips. Tarda unos minutos la primera vez.
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo  Instalando Python...
  winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
  echo.
  echo  Python instalado. Cierra esta ventana y vuelve a abrir instalar.bat para terminar.
  pause
  exit /b
)
python instalar.py
pause
