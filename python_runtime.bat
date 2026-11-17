@echo off
rem Use installed Python from PATH; no new virtual environment.
rem Explicit SEC_PYTHON_EXE overrides PATH. .python_exe is a fallback for scheduled jobs.
if defined SEC_PYTHON_EXE goto validate_python
for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable)" 2^>nul`) do if not defined SEC_PYTHON_EXE set "SEC_PYTHON_EXE=%%P"
if defined SEC_PYTHON_EXE goto validate_python
if not exist "%~dp0.python_exe" goto launcher_fallback
set /p SEC_PYTHON_EXE=<"%~dp0.python_exe"
if defined SEC_PYTHON_EXE if exist "%SEC_PYTHON_EXE%" goto validate_python
set "SEC_PYTHON_EXE="
:launcher_fallback
for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do if not defined SEC_PYTHON_EXE set "SEC_PYTHON_EXE=%%P"
:validate_python
if not defined SEC_PYTHON_EXE (
    echo ERROR: Cannot find Python. Try 'python --version' or specify SEC_PYTHON_EXE. 1>&2
    exit /b 1
)
if not exist "%SEC_PYTHON_EXE%" (
    echo ERROR: Python not found: "%SEC_PYTHON_EXE%". 1>&2
    exit /b 1
)
"%SEC_PYTHON_EXE%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.10 or newer is required. 1>&2
    exit /b 1
)
exit /b 0
