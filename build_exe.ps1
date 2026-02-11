param(
  [switch]$Clean
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
  throw "Virtualenv not found at $venvPython. Create it first: python -m venv .venv ; .\.venv\Scripts\python -m pip install -r requirements.txt"
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install --upgrade pyinstaller

if ($Clean) {
  $buildDir = Join-Path $root 'build'
  $distDir = Join-Path $root 'dist'
  if (Test-Path $buildDir) {
    try {
      Remove-Item -Recurse -Force $buildDir
    } catch {
      throw "Unable to delete $buildDir. Close any running client/server exe and retry. Details: $($_.Exception.Message)"
    }
  }
  if (Test-Path $distDir) {
    try {
      Remove-Item -Recurse -Force $distDir
    } catch {
      throw "Unable to delete $distDir (files might be in use). Close any running client/server exe (Task Manager) and retry. Details: $($_.Exception.Message)"
    }
  }
}

$baseArgs = @(
  '--noconfirm',
  '--clean'
)

Write-Host 'Building client...'
& $venvPython -m PyInstaller @baseArgs (Join-Path $root 'pyinstaller_client.spec')
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed building client (exit code $LASTEXITCODE)"
}

Write-Host 'Building server...'
& $venvPython -m PyInstaller @baseArgs (Join-Path $root 'pyinstaller_server.spec')
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed building server (exit code $LASTEXITCODE)"
}

Write-Host 'Done.'
Write-Host 'Outputs:'
$clientExe = Join-Path $root 'dist\client\client.exe'
$serverExe = Join-Path $root 'dist\server\server.exe'
if (-not (Test-Path $clientExe)) {
  throw "client.exe not found at $clientExe"
}
if (-not (Test-Path $serverExe)) {
  throw "server.exe not found at $serverExe"
}
Write-Host $clientExe
Write-Host $serverExe
