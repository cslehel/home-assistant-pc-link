@echo off
rem Double-click to uninstall Home Assistant PC Link (asks for administrator rights).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
