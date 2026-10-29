@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title Realtime Japanese Caption Translator

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE=%PROJECT_DIR%.venv\Scripts\python.exe"
set "INSTALL_MARKER=%PROJECT_DIR%.venv\.realtime-translator-installed"
set "ENV_FILE=%PROJECT_DIR%.env"
set "ENV_TEMPLATE=%PROJECT_DIR%.env.example"

cd /d "%PROJECT_DIR%"

if not exist "%PROJECT_DIR%main.py" (
    echo Startup failed: main.py was not found.
    echo Keep this launcher in the project root directory.
    echo.
    pause
    exit /b 2
)

if not exist "%PROJECT_DIR%requirements.txt" (
    echo Startup failed: requirements.txt was not found.
    echo Keep this launcher in the project root directory.
    echo.
    pause
    exit /b 2
)

if not exist "%PYTHON_EXE%" (
    set "BOOTSTRAP_PYTHON="

    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "BOOTSTRAP_PYTHON=py -3.12"

    if not defined BOOTSTRAP_PYTHON (
        py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
        if not errorlevel 1 set "BOOTSTRAP_PYTHON=py -3"
    )

    if not defined BOOTSTRAP_PYTHON (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
        if not errorlevel 1 set "BOOTSTRAP_PYTHON=python"
    )

    if not defined BOOTSTRAP_PYTHON (
        echo Python 3.12 or newer was not found.
        echo.
        echo Install Python 3.12 from https://www.python.org/downloads/windows/
        echo During installation, enable "Add python.exe to PATH", then try again.
        echo.
        pause
        exit /b 2
    )

    echo First-time setup: creating the project virtual environment...
    %BOOTSTRAP_PYTHON% -m venv "%PROJECT_DIR%.venv"
    if errorlevel 1 goto setup_failed

    if not exist "%PYTHON_EXE%" goto setup_failed
)

if not exist "%INSTALL_MARKER%" (
    echo First-time setup: installing project dependencies...
    "%PYTHON_EXE%" -m pip install --disable-pip-version-check -r "%PROJECT_DIR%requirements.txt"
    if errorlevel 1 goto setup_failed
    >"%INSTALL_MARKER%" echo installed
)

if not exist "%ENV_FILE%" (
    if not exist "%ENV_TEMPLATE%" (
        echo Startup failed: .env.example was not found.
        echo.
        pause
        exit /b 2
    )

    copy /y "%ENV_TEMPLATE%" "%ENV_FILE%" >nul
    echo 已从 .env.example 创建本地 .env 文件。
    echo 使用 DeepSeek 时，只需填写 API_KEY，保存并关闭 Notepad 即可。
    echo 使用其他 OpenAI 兼容模型时，请同时修改 .env 中的 API_BASE_URL 和 API_MODEL。
    start "" /wait notepad "%ENV_FILE%"
)

"%PYTHON_EXE%" "%PROJECT_DIR%main.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Startup or runtime failed. Exit code: %EXIT_CODE%
    echo Check the error above and the .env file in the project root.
    pause
)

endlocal & exit /b %EXIT_CODE%

:setup_failed
echo.
echo First-time setup failed.
echo Check the network connection and Python installation, then try again.
echo If the problem continues, delete the .venv directory and rerun this file.
echo.
pause
exit /b 2
