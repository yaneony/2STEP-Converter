@echo off
chcp 65001 > nul
setlocal EnableDelayedExpansion
title 2STEP-Converter
cd /d "%~dp0"

if exist "%~dp0lib" (
    set _MM_ROOT=%~dp0lib
) else if exist "%LOCALAPPDATA%\2STEP-Converter" (
    set _MM_ROOT=%LOCALAPPDATA%\2STEP-Converter
) else (
    echo No existing environment found. Where should the environment be installed?
    echo.
    echo  [1] Next to this script  ^(portable^)
    echo  [2] %LOCALAPPDATA%\2STEP-Converter
    echo.
    choice /c 12 /n /m "Your choice: "
    if errorlevel 2 (
        set _MM_ROOT=%LOCALAPPDATA%\2STEP-Converter
    ) else (
        set _MM_ROOT=%~dp0lib
    )
    echo.
)

set _LONGPATH=
for /f "tokens=3" %%V in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled 2^>nul') do set _LONGPATH=%%V
if not "!_LONGPATH!"=="0x1" (
    echo [WARNING] Windows 260-character path limit is not enabled.
    echo.
    echo  [1] Enable long paths and restart later  ^(recommended^)
    echo  [2] Install environment to %LOCALAPPDATA%\2STEP-Converter  ^(no restart needed^)
    echo.
    choice /c 12 /n /m "Your choice: "
    if errorlevel 2 (
        set _MM_ROOT=%LOCALAPPDATA%\2STEP-Converter
        echo Using: %LOCALAPPDATA%\2STEP-Converter
        echo.
    ) else (
        echo Requesting administrator access to enable long paths...
        powershell -NoProfile -Command "$p=Start-Process cmd -ArgumentList '/c reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f' -Verb RunAs -PassThru -Wait; exit $p.ExitCode"
        if errorlevel 1 (
            echo [ERROR] Long-path setting was not changed.
            pause & exit /b 1
        )
        echo.
        echo Long paths enabled. Restart Windows when convenient, then run this script again.
        pause & exit /b 0
    )
)

set _MM=!_MM_ROOT!\micromamba.exe
set _ENV=!_MM_ROOT!\env
set _PY=!_ENV!\python.exe
set _SPEC=%~dp0src\environment.yml
set _MM_SHA256=b645a5259cb92b5869b0e60943390dd0d362cae45bc7e2f5ba8c7e4a4b06c7aa
set MAMBA_ROOT_PREFIX=!_MM_ROOT!
set CONDA_PKGS_DIRS=!_MM_ROOT!
set PYTHONNOUSERSITE=1

set PATH=!_ENV!\Library\bin;!_ENV!\Library\mingw-w64\bin;!_ENV!\Scripts;!_ENV!;%PATH%

if exist "!_MM!" goto :check_env

if not exist "!_MM_ROOT!" mkdir "!_MM_ROOT!"
echo Downloading portable Python manager (one-time, ~10 MB) ...
curl.exe --ssl-no-revoke -L --progress-bar -o "!_MM!" "https://github.com/mamba-org/micromamba-releases/releases/download/2.8.1-1/micromamba-win-64.exe"
if errorlevel 1 (
    echo [ERROR] Download failed. Check your internet connection.
    pause & exit /b 1
)
set MM_VERIFY_PATH=!_MM!
set _HASH=
for /f "skip=1 delims=" %%H in ('%SystemRoot%\System32\certutil.exe -hashfile "!MM_VERIFY_PATH!" SHA256') do if not defined _HASH set "_HASH=%%H"
if not defined _HASH (
    echo [ERROR] Could not calculate the micromamba checksum.
    del /f /q "!_MM!" 2>nul
    pause & exit /b 1
)
set "_HASH=!_HASH: =!"
if /i not "!_HASH!"=="!_MM_SHA256!" (
    echo [ERROR] micromamba checksum verification failed.
    del /f /q "!_MM!" 2>nul
    pause & exit /b 1
)

:check_env
if not exist "!_SPEC!" (
    echo [ERROR] Missing environment specification: !_SPEC!
    pause & exit /b 1
)

set SPEC_VERIFY_PATH=!_SPEC!
set _SPEC_HASH=
for /f "skip=1 delims=" %%H in ('%SystemRoot%\System32\certutil.exe -hashfile "!SPEC_VERIFY_PATH!" SHA256') do if not defined _SPEC_HASH set "_SPEC_HASH=%%H"
if not defined _SPEC_HASH (
    echo [ERROR] Could not calculate the environment specification checksum.
    pause & exit /b 1
)
set "_SPEC_HASH=!_SPEC_HASH: =!"
set _SPEC_MARKER=!_ENV!\.2step-environment.sha256

if exist "!_PY!" (
    set _INSTALLED_SPEC_HASH=
    if exist "!_SPEC_MARKER!" set /p _INSTALLED_SPEC_HASH=<"!_SPEC_MARKER!"
    if /i not "!_INSTALLED_SPEC_HASH!"=="!_SPEC_HASH!" (
        echo Environment specification changed -- updating dependencies ...
        "!_MM!" install --prefix "!_ENV!" --file "!_SPEC!" --yes
        if errorlevel 1 (
            echo [ERROR] Failed to update the Python environment.
            pause & exit /b 1
        )
    )
    goto :check_deps
)

echo Setting up Python environment (one-time download, ~500 MB) ...
"!_MM!" create --prefix "!_ENV!" --file "!_SPEC!" --yes
if errorlevel 1 (
    echo [ERROR] Failed to create Python environment.
    pause & exit /b 1
)

:check_deps
"!_PY!" -c "from OCC.Core.StlAPI import StlAPI_Reader; import numpy, trimesh, networkx, fast_simplification, matplotlib, open3d, PIL" >nul 2>&1
if errorlevel 1 (
    echo Environment is incomplete or broken -- repairing ...
    "!_MM!" install --prefix "!_ENV!" --file "!_SPEC!" --force-reinstall --yes
    if errorlevel 1 (
        echo [ERROR] Failed to repair the Python environment.
        pause & exit /b 1
    )
    "!_PY!" -c "from OCC.Core.StlAPI import StlAPI_Reader; import numpy, trimesh, networkx, fast_simplification, matplotlib, open3d, PIL" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python environment is still broken after repair.
        pause & exit /b 1
    )
)

> "!_SPEC_MARKER!" echo !_SPEC_HASH!

:run
"!_PY!" "%~dp0src\converter.py" %*
