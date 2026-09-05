param(
    [string]$OutputRoot = "dist",
    [string]$PlatformToolsPath = "",
    [string]$ScrcpySourcePath = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$packageRoot = Join-Path $OutputRoot "PythonAdbController"
$stageRoot = Join-Path $OutputRoot ".portable-build"
$platformTools = if ($PlatformToolsPath) { (Resolve-Path -LiteralPath $PlatformToolsPath).Path } else { Join-Path $repoRoot "portable\platform-tools" }
$scrcpySource = if ($ScrcpySourcePath) { (Resolve-Path -LiteralPath $ScrcpySourcePath).Path } else { Join-Path $repoRoot "portable\scrcpy" }

if (Test-Path -LiteralPath $packageRoot) { throw "Output already exists: $packageRoot" }
foreach ($path in @($platformTools, $scrcpySource, (Join-Path $repoRoot "portable\config\config.toml"))) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required input missing: $path" }
}
if (-not $SkipTests) {
    & python -m unittest discover -s (Join-Path $repoRoot "adb_scrcpy\tests") -q
    if ($LASTEXITCODE -ne 0) { throw "Unit tests failed" }
}
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
$common = @("-m", "PyInstaller", "--noconfirm", "--clean", "--paths", $repoRoot, "--distpath", $OutputRoot, "--workpath", $stageRoot, "--specpath", $stageRoot)
& python @common --windowed --name "PythonAdbController" --collect-all cv2 --collect-all PIL --hidden-import numpy (Join-Path $repoRoot "launch_gui.py")
if ($LASTEXITCODE -ne 0) { throw "GUI build failed" }
& python @common --console --name "PythonAdbControllerCheck" (Join-Path $repoRoot "portable_check.py")
if ($LASTEXITCODE -ne 0) { throw "Diagnostic build failed" }
$guiDir = Join-Path $OutputRoot "PythonAdbController"
$checkDir = Join-Path $OutputRoot "PythonAdbControllerCheck"
Copy-Item -LiteralPath (Join-Path $checkDir "PythonAdbControllerCheck.exe") -Destination $guiDir
Copy-Item -LiteralPath (Join-Path $repoRoot "portable\config") -Destination $guiDir -Recurse
Copy-Item -LiteralPath (Join-Path $repoRoot "adb_scrcpy\workflows") -Destination $guiDir -Recurse
Copy-Item -LiteralPath $platformTools -Destination (Join-Path $guiDir "platform-tools") -Recurse
Copy-Item -LiteralPath $scrcpySource -Destination (Join-Path $guiDir "scrcpy") -Recurse
Copy-Item -LiteralPath (Join-Path $repoRoot "portable\README.md") -Destination (Join-Path $guiDir "README.md")
Copy-Item -LiteralPath (Join-Path $repoRoot "adb_scrcpy\TOOL_VERSIONS.md") -Destination (Join-Path $guiDir "TOOL_VERSIONS.md")
Copy-Item -LiteralPath (Join-Path $repoRoot "scripts\smoke_test_portable.ps1") -Destination (Join-Path $guiDir "smoke_test_portable.ps1")
New-Item -ItemType Directory -Path (Join-Path $guiDir "artifacts") -Force | Out-Null
$manifest = Join-Path $guiDir "checksums.sha256"
$files = Get-ChildItem -LiteralPath $guiDir -Recurse -File | Where-Object { $_.FullName -ne $manifest }
$files | ForEach-Object {
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower()
    $relative = $_.FullName.Substring($guiDir.Length + 1).Replace('\\', '/')
    "$hash  $relative"
} | Sort-Object | Set-Content -LiteralPath $manifest -Encoding ASCII
Write-Output "Portable package created: $guiDir"
