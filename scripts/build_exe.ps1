<#
.SYNOPSIS
  Builds the Windows installer into dist\.

.DESCRIPTION
  Produces, in dist\:
    py-intercom-setup.exe      (installer: Régie or Poste, picked at launch)
    py-intercom-licenses.zip   (LICENSE, third-party notices, Opus licence and build manifest)
    SHA256SUMS.txt
  The unpacked program (the installer's input) is moved to build\ afterwards,
  so dist\ holds exactly the released files.

  Inno Setup 6 is required (found on PATH or in Program Files; installed through
  Chocolatey when missing and Chocolatey is available).

  The same script runs locally and in CI. Locally, the .venv is created and
  filled on first use.

.PARAMETER Python
  Python interpreter to use. Defaults to .venv\Scripts\python.exe.

.PARAMETER Version
  Version stamped into the executables (shown in the window title).
  Defaults to the version in src\py_intercom\__init__.py.
#>
param(
  [string]$Python,
  [string]$Version
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dist = Join-Path $root 'dist'
$build = Join-Path $root 'build'
$versionFile = Join-Path $root 'src\py_intercom\__init__.py'

function Invoke-Checked([string]$what, [scriptblock]$command) {
  # Native tools log to stderr; judge them by exit code only (Windows PowerShell
  # would otherwise turn that output into terminating errors).
  $ErrorActionPreference = 'Continue'
  & $command
  if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

if (-not $Python) {
  $Python = Join-Path $root '.venv\Scripts\python.exe'
  if (-not (Test-Path $Python)) {
    Write-Host 'Creating .venv...'
    Invoke-Checked 'venv creation' { py -3 -m venv (Join-Path $root '.venv') }
    Invoke-Checked 'dependency installation' {
      & $Python -m pip install -r (Join-Path $root 'requirements.txt') -r (Join-Path $root 'requirements-dev.txt')
    }
  }
}

# PyInstaller silently skips a missing module: catch it here, not on the user's PC.
Invoke-Checked 'dependency check (run: pip install -r requirements.txt -r requirements-dev.txt)' {
  # find_spec, not import: opuslib needs opus.dll, which only the program prepares.
  & $Python -c "import importlib.util as u, sys; m = [n for n in ('cffi', 'loguru', 'numpy', 'opuslib', 'pynput', 'PySide6', 'sounddevice', 'PyInstaller') if u.find_spec(n) is None]; print('missing:', *m) if m else None; sys.exit(bool(m))"
}

foreach ($dir in @($build, $dist)) {
  if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
}

$utf8 = New-Object System.Text.UTF8Encoding($false)  # no BOM, on any PowerShell version
$originalVersionFile = [IO.File]::ReadAllText($versionFile)
try {
  if ($Version) {
    Write-Host "Stamping version $Version"
    $stamped = $originalVersionFile -replace '__version__ = "[^"]*"', "__version__ = `"$Version`""
    [IO.File]::WriteAllText($versionFile, $stamped, $utf8)
  }
  Invoke-Checked 'PyInstaller' {
    & $Python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $build (Join-Path $PSScriptRoot 'py-intercom.spec')
  }
} finally {
  [IO.File]::WriteAllText($versionFile, $originalVersionFile, $utf8)
}

function Find-InnoSetup {
  $found = Get-Command ISCC.exe -ErrorAction SilentlyContinue
  if ($found) { return $found.Source }
  foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, "$env:LOCALAPPDATA\Programs")) {
    if (-not $base) { continue }
    $candidate = Join-Path $base 'Inno Setup 6\ISCC.exe'
    if (Test-Path $candidate) { return $candidate }
  }
  return $null
}

$iscc = Find-InnoSetup
if (-not $iscc -and (Get-Command choco -ErrorAction SilentlyContinue)) {
  Write-Host 'Installing Inno Setup...'
  Invoke-Checked 'Inno Setup installation' { choco install innosetup -y --no-progress }
  $iscc = Find-InnoSetup
}
if (-not $iscc) { throw 'Inno Setup 6 not found: install it from https://jrsoftware.org/isdl.php' }

if (-not $Version) {
  $Version = ([regex]::Match($originalVersionFile, '__version__ = "([^"]*)"')).Groups[1].Value
}
$programDir = Join-Path $dist 'py-intercom'
Invoke-Checked 'Inno Setup' {
  & $iscc /Qp "/DAppVersion=$Version" "/DSourceDir=$programDir" "/DOutputDir=$dist" (Join-Path $PSScriptRoot 'py-intercom.iss')
}
# The unpacked folder is only the installer's input.
Move-Item $programDir (Join-Path $build 'py-intercom-unpacked') -Force

$licenses = Join-Path $build 'licenses'
New-Item -ItemType Directory -Force $licenses | Out-Null
Copy-Item (Join-Path $root 'LICENSE') (Join-Path $licenses 'LICENSE.txt')
Copy-Item (Join-Path $root 'THIRD_PARTY_NOTICES.md') $licenses
Copy-Item (Join-Path $root 'licenses\OPUS.txt') (Join-Path $licenses 'OPUS_LICENSE.txt')
Copy-Item (Join-Path $root 'bin\opus-build-info.json') (Join-Path $licenses 'OPUS_BUILD_INFO.json')
Compress-Archive -Path (Join-Path $licenses '*') -DestinationPath (Join-Path $dist 'py-intercom-licenses.zip') -Force

$sums = Get-ChildItem $dist -File | Sort-Object Name | ForEach-Object {
  '{0}  {1}' -f (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower(), $_.Name
}
Set-Content -Encoding ascii (Join-Path $dist 'SHA256SUMS.txt') $sums

Write-Host 'Built:'
Get-ChildItem $dist | ForEach-Object { Write-Host "  $($_.Name)" }
