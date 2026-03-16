param(
  [switch]$Clean
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
  $venvDir = Join-Path $projectRoot '.venv'

  $bootstrapPython = $null
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $bootstrapPython = @('py', '-3')
  } elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $bootstrapPython = @('python')
  }

  if (-not $bootstrapPython) {
    throw "Virtualenv not found at $venvPython and no Python launcher found (py/python). Install Python 3 or add it to PATH."
  }

  Write-Host "Creating virtualenv at $venvDir..."
  & $bootstrapPython[0] @($bootstrapPython[1..($bootstrapPython.Count-1)]) -m venv $venvDir

  if (-not (Test-Path $venvPython)) {
    throw "Virtualenv creation failed: $venvPython not found"
  }
}

$requirementsFile = Join-Path $projectRoot 'requirements.txt'

& $venvPython -m pip install --upgrade pip
if (Test-Path $requirementsFile) {
  & $venvPython -m pip install -r $requirementsFile
}
& $venvPython -m pip install --upgrade pyinstaller

if ($Clean) {
  $buildDir = Join-Path $projectRoot 'build'
  $distDir = Join-Path $projectRoot 'dist'
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

$clientSpec = Join-Path $scriptDir 'pyinstaller_client.spec'
$serverSpec = Join-Path $scriptDir 'pyinstaller_server.spec'
if (-not (Test-Path $clientSpec)) {
  throw "pyinstaller_client.spec not found at $clientSpec"
}
if (-not (Test-Path $serverSpec)) {
  throw "pyinstaller_server.spec not found at $serverSpec"
}

Push-Location $projectRoot
try {
Write-Host 'Building client...'
& $venvPython -m PyInstaller @baseArgs $clientSpec
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed building client (exit code $LASTEXITCODE)"
}

Write-Host 'Building server...'
& $venvPython -m PyInstaller @baseArgs $serverSpec
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed building server (exit code $LASTEXITCODE)"
}
} finally {
  Pop-Location
}

Write-Host 'Done.'
Write-Host 'Outputs:'
$clientExe = Join-Path $projectRoot 'dist\client.exe'
$serverExe = Join-Path $projectRoot 'dist\server.exe'
if (-not (Test-Path $clientExe)) {
  throw "client.exe not found at $clientExe"
}
if (-not (Test-Path $serverExe)) {
  throw "server.exe not found at $serverExe"
}
Write-Host $clientExe
Write-Host $serverExe
