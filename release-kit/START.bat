@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0python\pythonw.exe" (
  echo python folder is missing. Download the full ReleaseKit zip again.
  pause
  exit /b 1
)
if not exist "%~dp0output" mkdir "%~dp0output"
start "" "%~dp0python\pythonw.exe" "%~dp0app\releasekit.py" %*
