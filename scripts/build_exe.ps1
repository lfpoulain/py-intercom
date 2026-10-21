<#
.SYNOPSIS
  Builds the Windows executables into dist\.

.DESCRIPTION
  Produces, in dist\:
    py-intercom-server.exe
    py-intercom-client.exe
    py-intercom-licenses.zip   (LICENSE, third-party notices, Opus licence and build manifest)
    SHA256SUMS.txt

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
