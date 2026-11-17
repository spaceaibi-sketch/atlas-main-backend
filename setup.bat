@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1

echo [SEC Collector] Using your existing installed Python (no .venv)...
call "%~dp0python_runtime.bat"
if errorlevel 1 exit /b 1

echo Python: "%SEC_PYTHON_EXE%"
"%SEC_PYTHON_EXE%" --version

"%SEC_PYTHON_EXE%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: pip is unavailable for this Python. Try: "%SEC_PYTHON_EXE%" -m ensurepip --upgrade 1>&2
    exit /b 1
)

"%SEC_PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Dependency installation failed. 1>&2
    echo If this is a permissions error, install with --user or use an account with access. 1>&2
    exit /b 1
)

if not exist "assets" mkdir "assets"
if not exist "assets\key.txt" (
    type nul > "assets\key.txt"
    echo OPENAI: Created blank assets\key.txt. Paste your API key there to enable AI.
)
if not exist "..\data" mkdir "..\data"
if not exist "..\data\logs" mkdir "..\data\logs"
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo IMPORTANT: Open .env and replace SEC_USER_AGENT with your real email.
)

"%SEC_PYTHON_EXE%" -m sec_edgar.windows_launcher extract --init-db
if errorlevel 1 (
    echo ERROR: SQLite initialization failed. 1>&2
    exit /b 1
)

rem Persist the exact currently installed interpreter; scheduled tasks use the same one.
"%SEC_PYTHON_EXE%" -c "import pathlib,sys; pathlib.Path('.python_exe').write_text(sys.executable, encoding='utf-8')"
if errorlevel 1 (
    echo ERROR: Cannot save the selected interpreter in .python_exe. 1>&2
    exit /b 1
)

echo.
echo Setup complete. No new Python environment was created.
echo Edit .env, then run extract_recent_filings.bat, extract_forms_items.bat, extract_news.bat,
echo run_ai_sec_analys.bat or run_ai_news_analys.bat. View with dash.bat.
echo On Linux, use the matching .sh launchers and install_cron.sh.
exit /b 0
