@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
call "%~dp0python_runtime.bat"
if errorlevel 1 exit /b 1
"%SEC_PYTHON_EXE%" -u -m price_pipeline.cli %*
exit /b %ERRORLEVEL%
