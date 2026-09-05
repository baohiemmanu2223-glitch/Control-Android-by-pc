"""PyInstaller entry point for the portable GUI."""

from adb_scrcpy.gui import main


if __name__ == "__main__":
    raise SystemExit(main())
