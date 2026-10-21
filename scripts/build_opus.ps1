param(
  [switch]$Clean,
  [string]$WorkRoot,
  [string]$Destination,
  [string]$Generator = 'Visual Studio 17 2022',
  [string]$Architecture = 'x64'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$opusVersion = '1.6.1'
$sourceSha256 = '6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1'
$sourceArchiveName = "opus-$opusVersion.tar.gz"
$sourceUrl = "https://downloads.xiph.org/releases/opus/$sourceArchiveName"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
if (-not $WorkRoot) {
  $WorkRoot = Join-Path $projectRoot 'build\opus-runtime'
}
if (-not $Destination) {
  $Destination = Join-Path $projectRoot 'bin\opus.dll'
}

$workRootFull = [System.IO.Path]::GetFullPath($WorkRoot)
$destinationFull = [System.IO.Path]::GetFullPath($Destination)
$workRootDrive = [System.IO.Path]::GetPathRoot($workRootFull)
if ($workRootFull -eq $workRootDrive -or $workRootFull -eq $projectRoot) {
  throw "Unsafe Opus work directory: $workRootFull"
}

$archivePath = Join-Path $workRootFull $sourceArchiveName
$sourceDir = Join-Path $workRootFull "opus-$opusVersion"
$buildDir = Join-Path $workRootFull 'build-shared-x64'
$testBuildDir = Join-Path $workRootFull 'build-tests-x64'
$trackedLicense = Join-Path $projectRoot 'licenses\OPUS.txt'
$metadataPath = Join-Path $projectRoot 'bin\opus-build-info.json'

function Assert-LastExitCode([string]$operation) {
  if ($LASTEXITCODE -ne 0) {
    throw "$operation failed with exit code $LASTEXITCODE"
  }
}

function Get-Sha256([string]$path) {
  return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-PeMachine([string]$path) {
  $stream = [System.IO.File]::OpenRead($path)
  $reader = New-Object System.IO.BinaryReader($stream)
  try {
    if ($reader.ReadUInt16() -ne 0x5A4D) {
      throw "Not a PE binary: $path"
    }
    $stream.Seek(0x3C, [System.IO.SeekOrigin]::Begin) | Out-Null
    $peOffset = $reader.ReadInt32()
    $stream.Seek($peOffset, [System.IO.SeekOrigin]::Begin) | Out-Null
    if ($reader.ReadUInt32() -ne 0x00004550) {
      throw "Invalid PE signature: $path"
    }
    return $reader.ReadUInt16()
  } finally {
    $reader.Dispose()
    $stream.Dispose()
  }
}

if ($Clean -and (Test-Path -LiteralPath $workRootFull)) {
  Remove-Item -LiteralPath $workRootFull -Recurse -Force
}
New-Item -ItemType Directory -Path $workRootFull -Force | Out-Null

if (-not (Test-Path -LiteralPath $archivePath)) {
  Write-Host "Downloading official libopus $opusVersion source..."
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri $sourceUrl -OutFile $archivePath -UseBasicParsing
}

$actualSourceSha256 = Get-Sha256 $archivePath
if ($actualSourceSha256 -ne $sourceSha256) {
  throw "Opus source checksum mismatch. Expected $sourceSha256, got $actualSourceSha256"
}
Write-Host "Verified source SHA-256: $actualSourceSha256"

if (-not (Test-Path -LiteralPath $sourceDir)) {
  $tar = Get-Command tar -ErrorAction Stop
  & $tar.Source -xf $archivePath -C $workRootFull
  Assert-LastExitCode 'Opus source extraction'
}

$sourceLicense = Join-Path $sourceDir 'COPYING'
if (-not (Test-Path -LiteralPath $sourceLicense)) {
  throw "Official Opus licence not found: $sourceLicense"
}
if (-not (Test-Path -LiteralPath $trackedLicense)) {
  throw "Tracked Opus licence not found: $trackedLicense"
}
if ((Get-Sha256 $sourceLicense) -ne (Get-Sha256 $trackedLicense)) {
  throw 'The tracked Opus licence differs from COPYING in the verified source archive.'
}

$cmake = Get-Command cmake -ErrorAction Stop
$ctest = Get-Command ctest -ErrorAction Stop
$commonConfigureArgs = @(
  '-S', $sourceDir,
  '-G', $Generator,
  '-A', $Architecture,
  '-DOPUS_BUILD_PROGRAMS=OFF',
  '-DOPUS_HARDENING=ON',
  '-DOPUS_STATIC_RUNTIME=ON',
  '-DOPUS_CUSTOM_MODES=OFF',
  '-DOPUS_DEEP_PLC=ON',
  '-DOPUS_DRED=OFF',
  '-DOPUS_OSCE=OFF'
)
$sharedConfigureArgs = @(
  '-B', $buildDir,
  '-DOPUS_BUILD_SHARED_LIBRARY=ON',
  '-DOPUS_BUILD_TESTING=OFF'
) + $commonConfigureArgs
$testConfigureArgs = @(
  '-B', $testBuildDir,
  '-DOPUS_BUILD_SHARED_LIBRARY=OFF',
  '-DBUILD_SHARED_LIBS=OFF',
  '-DOPUS_BUILD_TESTING=ON'
) + $commonConfigureArgs

Write-Host 'Configuring verified x64 Opus runtime...'
& $cmake.Source @sharedConfigureArgs
Assert-LastExitCode 'Opus CMake configuration'

Write-Host 'Building the shared Opus runtime...'
& $cmake.Source --build $buildDir --config Release --parallel
Assert-LastExitCode 'Opus Release build'

Write-Host 'Configuring and building the official static Opus test suite...'
& $cmake.Source @testConfigureArgs
Assert-LastExitCode 'Opus test configuration'
& $cmake.Source --build $testBuildDir --config Release --parallel
Assert-LastExitCode 'Opus test build'
& $ctest.Source --test-dir $testBuildDir -C Release --output-on-failure
Assert-LastExitCode 'Opus tests'

$dllCandidates = @(
  (Join-Path $buildDir 'Release\opus.dll'),
  (Join-Path $buildDir 'opus.dll')
)
$builtDll = $dllCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $builtDll) {
  throw "Built opus.dll not found below $buildDir"
}
if ((Get-PeMachine $builtDll) -ne 0x8664) {
  throw 'The generated opus.dll is not an x64 PE binary.'
}

$destinationDirectory = Split-Path -Parent $destinationFull
New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
Copy-Item -LiteralPath $builtDll -Destination $destinationFull -Force
$dllSha256 = Get-Sha256 $destinationFull

$compilerId = ''
$compilerVersion = ''
$compilerMetadata = Get-ChildItem -LiteralPath (Join-Path $buildDir 'CMakeFiles') -Recurse -Filter CMakeCCompiler.cmake -ErrorAction SilentlyContinue | Select-Object -First 1
if ($compilerMetadata) {
  $compilerText = Get-Content -LiteralPath $compilerMetadata.FullName -Raw
  if ($compilerText -match 'set\(CMAKE_C_COMPILER_ID "([^"]+)"\)') {
    $compilerId = $Matches[1]
  }
  if ($compilerText -match 'set\(CMAKE_C_COMPILER_VERSION "([^"]+)"\)') {
    $compilerVersion = $Matches[1]
  }
}
$cmakeVersionOutput = & $cmake.Source --version
Assert-LastExitCode 'CMake version query'
$cmakeVersion = ($cmakeVersionOutput | Select-Object -First 1) -replace '^cmake version\s+', ''

$metadata = [ordered]@{
  schema_version = 1
  library = 'libopus'
  version = $opusVersion
  source_url = $sourceUrl
  source_sha256 = $actualSourceSha256
  dll_sha256 = $dllSha256
  architecture = 'x86_64'
  generator = $Generator
  compiler = $compilerId
  compiler_version = $compilerVersion
  cmake_version = $cmakeVersion
  configuration = 'Release'
  static_msvc_runtime = $true
  hardening = $true
  deep_plc = $true
  dred = $false
  osce = $false
  native_tests_passed = $true
  built_utc = [DateTime]::UtcNow.ToString('o')
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8

Write-Host "Installed verified Opus runtime: $destinationFull"
Write-Host "DLL SHA-256: $dllSha256"
Write-Host "Build metadata: $metadataPath"
