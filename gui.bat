@echo off
REM Abre a janela grafica do Storage Tier Manager (sem console).
REM Resolve o caminho COMPLETO do pythonw e usa Start-Process -FilePath (lida com o alias do WindowsApps).
cd /d "%~dp0"

set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYW set "PYW=%%i"

if not defined PYW (
    echo Python nao encontrado no PATH. Rode: python storage_tier_gui.py
    pause
    exit /b 1
)

powershell -NoProfile -Command "Start-Process -FilePath '%PYW%' -ArgumentList '%~dp0storage_tier_gui.py' -WorkingDirectory '%~dp0'"
