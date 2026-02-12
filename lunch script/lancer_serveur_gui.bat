@echo off
setlocal

set "ROOT=%~dp0.."
set "VENV=%ROOT%\.venv"

set "PYW=%VENV%\Scripts\pythonw.exe"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PYW%" (
    set "EXEC=%PYW%"
) else if exist "%PY%" (
    set "EXEC=%PY%"
) else (
    echo Virtualenv introuvable: "%VENV%"
    echo Cree-le avec: python -m venv .venv
    echo Puis: .\.venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)

start "py-intercom server" "%EXEC%" "%ROOT%\run_server.py" --gui
endlocal
