param([string]$PackageRoot = $PSScriptRoot)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path $PackageRoot).Path
foreach ($relative in @("PythonAdbController.exe", "PythonAdbControllerCheck.exe", "config\config.toml", "platform-tools\adb.exe", "scrcpy\scrcpy.exe", "scrcpy\scrcpy-server")) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $relative))) { throw "Portable file missing: $relative" }
}
Push-Location ([System.IO.Path]::GetTempPath())
try {
    & (Join-Path $root "PythonAdbControllerCheck.exe") --config (Join-Path $root "config\config.toml")
    if ($LASTEXITCODE -ne 0) { throw "Portable ADB diagnostic failed" }
    & (Join-Path $root "scrcpy\scrcpy.exe") --version
    if ($LASTEXITCODE -ne 0) { throw "Portable scrcpy version check failed" }
} finally { Pop-Location }
Write-Output "Portable smoke test passed: $root"
