@echo off
chcp 65001 >nul
rem Запуск Baltia Oracle на Windows: двойной щелчок по start.bat
rem Сам найдёт Python 3.10+, создаст окружение .venv, поставит зависимости
rem (заново - только если поменялся requirements.txt), проверит .env и запустит бота.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Baltia Oracle

rem ---- 1. Python 3.10 или новее ----
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto have_py
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto have_py
echo.
echo Не нашёл Python 3.10 или новее.
echo Поставь его с https://www.python.org/downloads/
echo В установщике обязательно отметь галочку «Add python.exe to PATH»,
echo потом запусти start.bat ещё раз.
goto fail

:have_py
rem ---- 2. Окружение .venv ----
rem живое окружение - это python и pip: без pip (оборвалась установка) зависимости не поставить
if not exist ".venv\Scripts\python.exe" goto make_venv
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if not errorlevel 1 goto have_venv
echo Окружение .venv сломано - пересоздаю.
rmdir /s /q ".venv"

:make_venv
if exist ".venv" rmdir /s /q ".venv"
echo Создаю окружение .venv...
%PY% -m venv .venv
if not errorlevel 1 goto venv_made
rem недоделанное окружение не оставляем - иначе следующий запуск примет его за готовое
if exist ".venv" rmdir /s /q ".venv"
goto venv_fail

:venv_made
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check --upgrade pip >nul 2>&1

:have_venv
rem ---- 3. Зависимости (только если requirements.txt поменялся) ----
fc /b "requirements.txt" ".venv\.requirements.installed" >nul 2>&1
if not errorlevel 1 goto deps_ok
echo Ставлю зависимости, первый раз это минута-две...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto deps_fail
copy /y "requirements.txt" ".venv\.requirements.installed" >nul

:deps_ok
rem ---- 4. Настройки .env ----
if exist ".env" goto have_env
copy ".env.example" ".env" >nul
echo.
echo Создал файл .env с настройками и открываю его в Блокноте.
echo Впиши как минимум:
echo   BOT_TOKEN          - токен бота от @BotFather
echo   DEEPSEEK_API_KEY   - ключ с platform.deepseek.com
echo   GROQ_API_KEY       - для голосовых, бесплатно: console.groq.com
echo Сохрани файл и запусти start.bat ещё раз.
start "" notepad ".env"
goto fail

:have_env
if defined BOT_TOKEN goto token_ok
findstr /r /c:"^ *BOT_TOKEN *= *[0-9a-zA-Z]" ".env" >nul 2>&1
if not errorlevel 1 goto token_ok
echo.
echo В .env пустой BOT_TOKEN - возьми токен у @BotFather командой /newbot и впиши.
goto fail

:token_ok
if defined DEEPSEEK_API_KEY goto key_ok
if defined LLM_API_KEY goto key_ok
findstr /r /c:"^ *DEEPSEEK_API_KEY *= *[0-9a-zA-Z]" ".env" >nul 2>&1
if not errorlevel 1 goto key_ok
findstr /r /c:"^ *LLM_API_KEY *= *[0-9a-zA-Z]" ".env" >nul 2>&1
if not errorlevel 1 goto key_ok
echo.
echo В .env пустой DEEPSEEK_API_KEY - создай ключ на platform.deepseek.com в разделе API keys и впиши.
goto fail

:key_ok
if defined OWNER_ID goto owner_ok
findstr /r /c:"^ *OWNER_ID *= *[0-9]" ".env" >nul 2>&1
if not errorlevel 1 goto owner_ok
echo OWNER_ID пока пустой: напиши боту /start - он пришлёт твой id. Впиши его в .env и перезапусти.

:owner_ok
rem ---- 5. Локальный whisper, если выбран ----
findstr /r /i /c:"^ *STT_PROVIDER *= *local" ".env" >nul 2>&1
if errorlevel 1 goto voice_ok
".venv\Scripts\python.exe" -c "import faster_whisper" >nul 2>&1
if not errorlevel 1 goto voice_ok
echo STT_PROVIDER=local - ставлю faster-whisper, это надолго: пара сотен МБ...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements-voice.txt
if errorlevel 1 echo faster-whisper не поставился - голосовые пока работать не будут.

:voice_ok
rem ---- 6. Запуск ----
if not exist "data" mkdir "data"
echo Запускаю бота. Остановить - Ctrl+C или закрыть это окно.
echo Пока окно открыто и компьютер не спит, бот работает.
".venv\Scripts\python.exe" -m oracle %*
if errorlevel 1 goto crashed
endlocal
exit /b 0

:crashed
echo.
echo Бот остановился с ошибкой - её текст выше.
echo Частые причины и что делать - в README, раздел «Если что-то не так».
goto fail

:venv_fail
echo.
echo Не смог создать окружение .venv. Переустанови Python с python.org и запусти ещё раз.
goto fail

:deps_fail
echo.
echo Зависимости не поставились - проверь интернет и запусти start.bat ещё раз.
goto fail

:fail
echo.
pause
endlocal
exit /b 1
