@echo off
setlocal

set "ROOT=%~dp0.."
set "VENV=%ROOT%\.venv"

set "PYW=%VENV%\Scripts\pythonw.exe"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
    echo Virtualenv introuvable: "%VENV%"
    echo Creation du venv...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv "%VENV%"
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 (
            python -m venv "%VENV%"
        ) else (
            echo Python introuvable (py/python). Installe Python 3 et relance.
            pause
            exit /b 1
        )
    )
)

if not exist "%PY%" (
    echo Echec creation du virtualenv: "%VENV%"
    pause
    exit /b 1
)

set "REQ=%ROOT%\requirements.txt"
if exist "%REQ%" (
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r "%REQ%"
)

if exist "%PYW%" (
    set "EXEC=%PYW%"
) else (
    set "EXEC=%PY%"
)

start "py-intercom client" "%EXEC%" "%ROOT%\run_client.py" --gui
endlocal
