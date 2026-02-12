@echo off
setlocal

set "ROOT=%~dp0.."
set "VENV=%ROOT%\.venv"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" goto :venv_ok

echo Virtualenv introuvable: "%VENV%"
echo Creation du venv...

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 -m venv "%VENV%"
    goto :check_venv
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    python -m venv "%VENV%"
    goto :check_venv
)

echo Python introuvable (py/python). Installe Python 3 et relance.
pause
exit /b 1

:check_venv
if not exist "%PY%" (
    echo Echec creation du virtualenv: "%VENV%"
    pause
    exit /b 1
)

echo Installation des dependances...
"%PY%" -m pip install --upgrade pip
if exist "%ROOT%\requirements.txt" "%PY%" -m pip install -r "%ROOT%\requirements.txt"

:venv_ok
rem Fenetre CLI visible (debug): on ouvre un nouveau cmd qui reste ouvert (/k)
start "py-intercom server debug" cmd /k "\"%PY%\" \"%ROOT%\run_server.py\" --gui --debug"
endlocal
