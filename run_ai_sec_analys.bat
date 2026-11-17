@echo off
rem Compatibility alias. Prefer run_ai_sec_scoring.bat.
call "%~dp0run_ai_sec_scoring.bat" %*
exit /b %ERRORLEVEL%
