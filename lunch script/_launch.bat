@echo off
setlocal

set "COMPONENT=%~1"
set "MODE=%~2"
if /I "%COMPONENT%"=="client" goto :component_ok
if /I "%COMPONENT%"=="server" goto :component_ok
echo Composant invalide: "%COMPONENT%". Valeurs attendues: client ou server.
exit /b 2

:component_ok
set "ROOT=%~dp0.."
set "VENV=%ROOT%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"

if exist "%PY%" goto :check_python

echo Virtualenv introuvable: "%VENV%"
echo Creation du venv...
where py >nul 2>nul
if not errorlevel 1 goto :create_with_py
where python >nul 2>nul
if not errorlevel 1 goto :create_with_python
echo Python 3.11 ou plus recent est introuvable.
pause
exit /b 1

:create_with_py
py -3 -m venv "%VENV%"
if errorlevel 1 goto :venv_failed
goto :install

:create_with_python
python -m venv "%VENV%"
if errorlevel 1 goto :venv_failed
goto :install

:venv_failed
echo Echec de creation du virtualenv: "%VENV%"
pause
exit /b 1

:install
if not exist "%PY%" goto :venv_failed
call :require_python
if errorlevel 1 exit /b 1

echo Installation des dependances...
"%PY%" -m pip install --upgrade pip
if errorlevel 1 goto :install_failed
"%PY%" -m pip install -r "%ROOT%\requirements.txt"
if errorlevel 1 goto :install_failed
goto :launch

:check_python
call :require_python
if errorlevel 1 exit /b 1
"%PY%" -c "import importlib.util as u; names=('loguru','numpy','opuslib','pynput','PySide6','sounddevice'); raise SystemExit(0 if all(u.find_spec(n) for n in names) else 1)"
if errorlevel 1 goto :install
goto :launch

:require_python
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if not errorlevel 1 exit /b 0
echo Le virtualenv utilise une version de Python trop ancienne.
echo Supprime "%VENV%" puis installe Python 3.11 ou plus recent.
pause
exit /b 1

:install_failed
echo Echec de l'installation des dependances.
pause
exit /b 1

:launch
if /I "%MODE%"=="debug" goto :launch_debug
if not exist "%PYW%" set "PYW=%PY%"
start "py-intercom %COMPONENT%" /D "%ROOT%" "%PYW%" "%ROOT%\run_%COMPONENT%.py"
if errorlevel 1 (
    echo Echec du lancement de py-intercom %COMPONENT%.
    pause
    exit /b 1
)
exit /b 0

:launch_debug
pushd "%ROOT%"
"%PY%" "%ROOT%\run_%COMPONENT%.py" --debug
set "APP_EXIT=%ERRORLEVEL%"
popd
if not "%APP_EXIT%"=="0" echo py-intercom %COMPONENT% s'est termine avec le code %APP_EXIT%.
pause
exit /b %APP_EXIT%
