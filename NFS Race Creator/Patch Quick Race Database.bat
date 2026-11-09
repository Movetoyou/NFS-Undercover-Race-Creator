@echo off
setlocal
py -3 -B "%~dp0Patch Quick Race Database.py" %*
set "PATCH_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %PATCH_EXIT%
