# Python ADB Controller

Desktop controller for Android devices using Python, ADB and scrcpy. The app supports device discovery, Focus View, safe ADB input, workflow replay, repeat loops, recorder, file transfer, APK installation, logs and Game Safe Mode.

## Requirements

- Windows 10/11 for the GUI recorder and portable build.
- Python 3.11 or newer.
- Android Platform-Tools (`adb.exe`).
- scrcpy 4.1 or a compatible release.
- USB debugging enabled and RSA authorization accepted on the device.

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Run from source

Copy `adb_scrcpy/config.example.toml` to a local config file and set the device serial, ADB path and scrcpy path. Do not commit a config containing real serials or machine-specific paths.

```powershell
python launch_gui.py --config path\to\config.toml
```

The default workflow safety is Dry-run. To send live input, turn Dry-run off, enable Confirm input, and confirm the prompt. Leave Target app package blank for coordinate-based workflows that should run on the currently visible app; set it only when app-specific health/launch behavior is required.

## Build portable distribution

The build script expects Platform-Tools and scrcpy sources in the repository layout used by development. It produces a folder distribution containing the GUI, diagnostic checker, config, workflows, ADB and scrcpy.

```powershell
./scripts/build_portable.ps1 -OutputRoot .\dist `
  -PlatformToolsPath C:\Android\platform-tools `
  -ScrcpySourcePath C:\Tools\scrcpy-win64-v4.1
./scripts/smoke_test_portable.ps1 -PackageRoot .\dist\PythonAdbController
```

For a GitHub checkout, pass the local Platform-Tools and scrcpy directories with the two parameters above. The tracked `portable/config/config.toml` is an intentionally generic template; replace its placeholders locally before distributing the built package. Keep the portable directory structure intact when distributing the result.

## Safety

- Use Dry-run before live replay.
- Batch input is not broadcast by default.
- Emergency stop and Game Safe Mode stop activity managed by this application.
- Test automation on a dedicated device/account and respect each app's terms of service.
- Never commit `artifacts/`, credentials, real device serials or proprietary APKs.

## Tests

```powershell
python -m unittest discover -s adb_scrcpy\tests -q
```

See `docs/` for the phased implementation and UI roadmap.
