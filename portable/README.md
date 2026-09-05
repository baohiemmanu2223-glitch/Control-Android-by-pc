# Python ADB Controller Portable

1. Enable Developer options and USB debugging on Android.
2. Connect a data-capable USB cable, unlock the device, and accept the RSA prompt.
3. Run `PythonAdbController.exe`.
4. Select the device, then use Check Device or Open scrcpy.

The folder carries its own ADB and scrcpy in a built distribution. Keep the directory structure intact.
Copy `config/config.example.toml` to `config/config.toml` and edit the serial, package and tool paths for your machine before running a portable build.
Run `PythonAdbControllerCheck.exe` from Command Prompt for diagnostics.

When finished, revoke USB debugging authorizations and disable USB debugging.
