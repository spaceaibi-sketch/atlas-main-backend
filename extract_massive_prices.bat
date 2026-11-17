@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Fetch Massive 15-minute prices for all companies (default 7 days).
cd /d "%~dp0" || exit /b 1
call "%~dp0python_runtime.bat"
if errorlevel 1 exit /b 1
"%SEC_PYTHON_EXE%" -u -m price_pipeline.massive_cli %*
exit /b %ERRORLEVEL%
