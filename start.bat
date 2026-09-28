@echo off
REM PYTHIA v5.1 "SOVET" - Windows launcher (ASCII only: Cyrillic inside a .bat
REM shifts cmd's read position and commands get cut mid-line).
REM Order: python -> venv -> DEPENDENCIES FIRST (visible, checked, retried) -> mode -> data -> start.
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist data mkdir data
set "LOG=data\install.log"
del "%LOG%" >nul 2>nul

echo ==========================================================
echo   PYTHIA v5.1 SOVET  -  news ^> council ^> your view ^> mission
echo ==========================================================
echo.

REM ---------- 0. cloud-synced folder? (OneDrive etc. lock files while pip installs) ----------
echo %CD% | findstr /i "OneDrive Yandex Google Dropbox iCloud" >nul
if not errorlevel 1 (
  echo [PYTHIA] WARNING: the project sits inside a cloud-synced folder:
  echo          %CD%
  echo          OneDrive / Yandex Disk / Google Drive lock files while pip installs,
  echo          and .venv gets damaged. Best: move the folder to C:\pythia
  echo          ^(or pause syncing^), delete .venv and run start.bat again.
  echo.
)

REM ---------- 1. python 3.10+ ----------
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [PYTHIA] Python not found. Install Python 3.12 from python.org and tick
  echo          "Add python.exe to PATH" during setup. Then run start.bat again.
  pause
  exit /b 1
)
set "PYVER="
for /f "tokens=2 delims= " %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
if "%PYVER:~0,2%" NEQ "3." (
  echo [PYTHIA] "%PY%" is not a working Python 3 ^(got: %PYVER%^). If this is the Microsoft
  echo          Store stub, install Python 3.12 from python.org with "Add to PATH".
  pause
  exit /b 1
)
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (set "PYMAJ=%%a" & set "PYMIN=%%b")
echo [PYTHIA] Python %PYVER% ^(%PY%^)
if %PYMIN% LSS 10 (
  echo [PYTHIA] Python 3.10 or newer is required. Install Python 3.12 from python.org.
  pause
  exit /b 1
)

REM ---------- 2. venv (recreated if broken) ----------
set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" (
  "%VPY%" -c "import sys" >nul 2>nul
  if errorlevel 1 (
    echo [PYTHIA] .venv is broken - recreating...
    rmdir /s /q .venv
  )
)
if not exist "%VPY%" (
  echo [PYTHIA] creating virtual environment .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [PYTHIA] venv creation FAILED - see the error above.
    echo          If the folder is in OneDrive/Yandex Disk - move it to C:\pythia.
    pause
    exit /b 1
  )
)

REM ---------- 3. dependencies FIRST: visible, logged, checked, retried ----------
echo [PYTHIA] installing dependencies ^(log: %LOG%^). First run takes a minute...
"%VPY%" -m pip install --upgrade pip --prefer-binary --disable-pip-version-check 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%' -Append"
set "TRY=0"
:pip_retry
set /a TRY+=1
echo [PYTHIA] pip install -r requirements.txt  ^(attempt %TRY% of 3^)
"%VPY%" -m pip install --prefer-binary --disable-pip-version-check -r requirements.txt 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%' -Append"
"%VPY%" -c "import fastapi, uvicorn, httpx, feedparser, openai, pydantic, websockets, skyfield, numpy, jplephem" 2>>"%LOG%"
if errorlevel 1 (
  if %TRY% LSS 3 (
    echo [PYTHIA] some packages are missing or broken - retrying...
    goto pip_retry
  )
  echo [PYTHIA] dependencies still do not import after 3 attempts. Recreating .venv once...
  rmdir /s /q .venv
  %PY% -m venv .venv
  "%VPY%" -m pip install --prefer-binary --disable-pip-version-check -r requirements.txt 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%' -Append"
  "%VPY%" -c "import fastapi, uvicorn, httpx, feedparser, openai, pydantic, websockets, skyfield, numpy, jplephem" 2>>"%LOG%"
  if errorlevel 1 (
    echo.
    echo [PYTHIA] INSTALL FAILED. Last lines of %LOG%:
    powershell -NoProfile -Command "Get-Content -Tail 30 '%LOG%'"
    echo.
    echo   Usual causes: no internet; antivirus or cloud sync locking .venv
    echo   ^(move the folder to C:\pythia^); Python too new for a package
    echo   ^(install Python 3.12 and delete .venv^). Send this window's text to the developer.
    pause
    exit /b 1
  )
)
echo [PYTHIA] dependencies OK

REM ---------- 4. mode ----------
echo.
echo   1 = NORMAL   (keys from the panel; with Tinkoff token = REAL orders)
echo   2 = DRY RUN  (everything works, orders only go to data\trader_audit.jsonl)
echo   3 = DEMO     (no keys, no internet: fake AI, news and broker)
echo.
set "MODE=2"
set /p MODE="Choose mode [1/2/3] and press Enter (default 2 = dry run): "
if "%MODE%"=="1" goto mode_ok
if "%MODE%"=="2" goto mode_ok
if "%MODE%"=="3" goto mode_ok
set "MODE=2"
:mode_ok
if "%MODE%"=="2" set "PYTHIA_DRY=1"
if "%MODE%"=="3" set "PYTHIA_MOCK_AI=1"
if "%MODE%"=="3" set "PYTHIA_MOCK_TINKOFF=1"
if "%MODE%"=="3" set "PYTHIA_DRY=1"

REM ---------- 5. data files (ephemeris ~32 MB once; not needed in demo) ----------
if not "%MODE%"=="3" (
  echo [PYTHIA] checking data files ^(ephemeris for astro/ether^)...
  "%VPY%" -m backend.fetch_data
  if errorlevel 1 (
    echo [PYTHIA] WARNING: data download failed - astro/ether run in reduced mode.
    echo          You can copy de440s.bsp from the old version into data\ and restart.
  )
)

REM ---------- 6. the server must import before we start it ----------
set "RS_NO_BROWSER=1"
"%VPY%" -c "from backend import server"
if errorlevel 1 (
  echo.
  echo [PYTHIA] the server does not start - the reason is the traceback above.
  echo          Send this window's text to the developer.
  pause
  exit /b 1
)
set "RS_NO_BROWSER="

REM ---------- 7. port free? ----------
if "%PORT%"=="" set "PORT=8799"
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo [PYTHIA] WARNING: port %PORT% is already in use ^(another PYTHIA is running?^).
  echo          Close it, or set PORT=8800 in this window and run start.bat again.
  echo          Continuing anyway - the server may fail to bind.
)

REM ---------- 8. start ----------
if "%MODE%"=="1" echo [PYTHIA] mode: NORMAL - with a Tinkoff token orders are REAL
if "%MODE%"=="2" echo [PYTHIA] mode: DRY RUN - orders are NOT sent to the exchange
if "%MODE%"=="3" echo [PYTHIA] mode: DEMO - fake AI, news and broker
echo [PYTHIA] starting at http://localhost:%PORT%  ^(Ctrl+C stops the server^)
start "" cmd /c "timeout /t 6 >nul & start http://localhost:%PORT%"
"%VPY%" -m uvicorn backend.server:app --host 0.0.0.0 --port %PORT%
echo.
echo [PYTHIA] server stopped. If it crashed, the reason is above - send this window's text.
pause
