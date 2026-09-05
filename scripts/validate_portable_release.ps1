param(
    [string]$PackageRoot = ".\dist\PythonAdbController",
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $PackageRoot).Path
$required = @(
    "PythonAdbController.exe",
    "PythonAdbControllerCheck.exe",
    "config\config.toml",
    "platform-tools\adb.exe",
    "platform-tools\AdbWinApi.dll",
    "platform-tools\AdbWinUsbApi.dll",
    "scrcpy\scrcpy.exe",
    "scrcpy\scrcpy-server",
    "scrcpy\SDL3.dll",
    "workflows\device_smoke.json"
)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $root $_)) })
$manifest = Join-Path $root "checksums.sha256"
$mismatches = @()
if (Test-Path -LiteralPath $manifest) {
    foreach ($line in Get-Content -LiteralPath $manifest) {
        if ($line -match '^([0-9a-fA-F]{64})  (.+)$') {
            $path = Join-Path $root $matches[2]
            if (-not (Test-Path -LiteralPath $path)) { $mismatches += $matches[2]; continue }
            $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $matches[1].ToLower()) { $mismatches += $matches[2] }
        }
    }
} else {
    $mismatches += "checksums.sha256 (missing)"
}
$scrcpyVersion = (& (Join-Path $root "scrcpy\scrcpy.exe") --version 2>&1 | Select-Object -First 1).ToString().Trim()
$payload = [ordered]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    package_root = $root
    required_files = $required.Count
    missing_files = $missing
    checksum_mismatches = $mismatches
    scrcpy_version = $scrcpyVersion
    status = if ($missing.Count -eq 0 -and $mismatches.Count -eq 0) { "passed" } else { "failed" }
}
if ($ReportPath) {
    $report = [IO.Path]::GetFullPath($ReportPath)
    New-Item -ItemType Directory -Path (Split-Path -Parent $report) -Force | Out-Null
    $payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $report -Encoding UTF8
}
$payload | ConvertTo-Json -Depth 5
if ($payload.status -ne "passed") { exit 1 }
