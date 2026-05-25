@echo off
REM v1.10 AgentsHive Desktop installer builder (Windows).
REM
REM Two-step build:
REM   1. PyInstaller --onedir compiles src/agentshive/desktop.py + the agentshive
REM      package + templates into dist/AgentsHive/ (folder of EXE + DLLs + Python
REM      stdlib + dependencies).
REM   2. Inno Setup compiler (iscc.exe) reads scripts/AgentsHive.iss and packs
REM      the dist/AgentsHive/ tree into dist/AgentsHive-Setup-1.10.0.exe.
REM
REM Prerequisites:
REM   - Python 3.11+ with .venv/Scripts/python.exe and `pip install -e .[desktop]`
REM     run already (provides pywebview + pyinstaller).
REM   - Inno Setup 6 installed from https://jrsoftware.org/isinfo.php
REM     (the iscc.exe compiler must be on PATH OR at the default install path
REM     "C:\Program Files (x86)\Inno Setup 6\iscc.exe").
REM
REM Output:
REM   dist/AgentsHive/            — unpacked PyInstaller bundle (debug/local-run)
REM   dist/AgentsHive-Setup-1.10.0.exe — installer that end users download.
REM
REM SmartScreen note: the installer is UNSIGNED (no $300/yr EV cert in v1.10
REM per Planner Q5). First-time users see "Windows protected your PC" — they
REM click "More info" → "Run anyway". Documented in the README.

setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo === [1/3] PyInstaller bundle (--onedir, --windowed) ===
REM --onedir per Planner Q6 (faster startup, no temp-extract AV trip, easier
REM to debug missing files than --onefile). The Inno Setup step still produces
REM a single end-user .exe, so onedir is invisible to the user.
REM --add-data: explicit path inclusion for the HTML templates per the v1.5
REM packaging lesson. PyInstaller doesn't auto-discover non-.py resources.
.venv\Scripts\python.exe -m PyInstaller ^
  --windowed ^
  --onedir ^
  --name AgentsHive ^
  --noconfirm ^
  --clean ^
  --add-data "src/agentshive/templates;agentshive/templates" ^
  --collect-all fastmcp ^
  --collect-all mcp ^
  --collect-all sqlmodel ^
  --collect-all fastapi ^
  --copy-metadata fastmcp-slim ^
  --hidden-import uvicorn.lifespan.on ^
  --hidden-import uvicorn.lifespan.off ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.http.h11_impl ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.loops.asyncio ^
  src/agentshive/desktop.py
if errorlevel 1 (
  echo PyInstaller failed.
  exit /b 1
)
echo === PyInstaller OK; output in dist\AgentsHive\ ===

echo === [2/3] Locate Inno Setup compiler ===
set ISCC_EXE=
where iscc.exe 1>nul 2>nul && set ISCC_EXE=iscc.exe
if "%ISCC_EXE%"=="" if exist "C:\Program Files (x86)\Inno Setup 6\iscc.exe" set ISCC_EXE="C:\Program Files (x86)\Inno Setup 6\iscc.exe"
if "%ISCC_EXE%"=="" if exist "C:\Program Files\Inno Setup 6\iscc.exe" set ISCC_EXE="C:\Program Files\Inno Setup 6\iscc.exe"
if "%ISCC_EXE%"=="" (
  echo ERROR: iscc.exe ^(Inno Setup compiler^) not found.
  echo Install Inno Setup 6 from https://jrsoftware.org/isinfo.php
  echo then re-run this script.
  exit /b 2
)
echo Using compiler: %ISCC_EXE%

echo === [3/3] Inno Setup compile ===
%ISCC_EXE% scripts\AgentsHive.iss
if errorlevel 1 (
  echo Inno Setup compilation failed.
  exit /b 3
)

echo === BUILD COMPLETE ===
echo Installer: dist\AgentsHive-Setup-1.10.0.exe
echo Test the installer on a clean account or VM before publishing.
endlocal
