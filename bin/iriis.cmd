@echo off
rem Iriis CLI for cmd.exe / PowerShell. Add this folder to PATH, then: iriis status
set "IRIIS_DIR=%~dp0.."
set "PYTHONPATH=%IRIIS_DIR%"
set "PYTHONIOENCODING=utf-8"
"%IRIIS_DIR%\.venv\Scripts\python.exe" -m iriis.cli %*
