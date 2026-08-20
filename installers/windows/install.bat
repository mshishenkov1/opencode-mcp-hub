@echo off
rem Thin launcher: runs install.ps1 next to this file and returns its exit code (N5-I2).
rem All messages live in install.ps1; this file is ASCII only (N5-T6).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %ERRORLEVEL%
