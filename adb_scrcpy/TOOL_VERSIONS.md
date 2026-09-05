# Tool Versions

| Tool | Locked/observed version | Status |
|---|---:|---|
| Python | 3.11.9 | verified |
| Platform-Tools / ADB | 37.0.0-14910828 / ADB 1.0.41 | verify locally before release |
| scrcpy | 4.1 | verify locally before release |

## Checksums

- `scrcpy-win64-v4.1.zip`: SHA-256 `5b12172b3264b2889f4583ee64752ce832e29bc8b1089dca81093459697165db`
- `scrcpy.exe`: SHA-256 `575ca1284345c7b3975585bc61b66d564a9a4f1ecb28fbb4c599c92a124054a9`

## Smoke Test

```powershell
$env:ADB = "C:\\Android\\platform-tools\\adb.exe"
C:\\Tools\\scrcpy\\scrcpy.exe --serial=REPLACE_WITH_DEVICE_SERIAL --no-audio --no-playback --time-limit=2 --no-window
```

Expected: scrcpy connects to the authorized test device, then exits with code `0` after the time limit.

Do not replace `adb.exe` or scrcpy silently in a production run. Re-run the
device smoke test after changing either tool.
