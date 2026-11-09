@echo off
setlocal
cd /d "%~dp0"

rem Diagnostics and verbose runs need console Python so the report/log is visible.
echo %* | findstr /i /c:"--diagnostics" >nul && goto console
echo %* | findstr /i /c:"--verbose" >nul && goto console

where pyw >nul 2>nul
if errorlevel 1 goto console
pyw -3 "nfs-race-creator.py" %*
if errorlevel 1 goto console
goto end

:console
py -3 "nfs-race-creator.py" %*

:end
endlocal
