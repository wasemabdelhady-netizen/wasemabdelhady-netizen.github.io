@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0python\pythonw.exe" (
  echo python folder is missing. Download the full PitchTrainer zip again.
  pause
  exit /b 1
)
start "" "%~dp0python\pythonw.exe" "%~dp0app\pitchtrainer.py" %*
