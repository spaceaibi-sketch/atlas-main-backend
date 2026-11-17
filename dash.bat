@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
call "%~dp0python_runtime.bat"
if errorlevel 1 exit /b 1
echo Starting SEC Dash Explorer with "%SEC_PYTHON_EXE%".
echo Open http://127.0.0.1:8050 in your browser.
echo Press Ctrl+C to stop the dashboard.
"%SEC_PYTHON_EXE%" -m sec_edgar.windows_launcher dash %*
exit /b %ERRORLEVEL%
