@echo off
setlocal enabledelayedexpansion
REM agentchattr — starts server (if not running) + Claude wrapper (auto-approve mode)
set "ORIG_PWD=%CD%"
cd /d "%~dp0.."

REM Auto-create venv and install deps on first run
if not exist ".venv" (
    python -m venv .venv
    .venv\Scripts\pip install -q -r requirements.txt >nul 2>nul
)
call .venv\Scripts\activate.bat

REM Pre-flight: check that claude CLI is installed
where claude >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Error: "claude" was not found on PATH.
    echo   Install it first, then try again.
    echo.
    pause
    exit /b 1
)

REM --- Parse project flags ---
set "ARG_PROJECT="
set "ARG_PROJECT_NAME="
set "ARG_PORT="
set "ARG_MCP_HTTP_PORT="
set "ARG_MCP_SSE_PORT="
set "ARG_ARTIFACT_ROOT="

:parse_args
if "%~1"=="" goto :done_args
if "%~1"=="--project"         ( set "ARG_PROJECT=%~2"& shift & shift & goto :parse_args )
if "%~1"=="--project-name"    ( set "ARG_PROJECT_NAME=%~2"& shift & shift & goto :parse_args )
if "%~1"=="--port"            ( set "ARG_PORT=%~2"& shift & shift & goto :parse_args )
if "%~1"=="--mcp-http-port"   ( set "ARG_MCP_HTTP_PORT=%~2"& shift & shift & goto :parse_args )
if "%~1"=="--mcp-sse-port"    ( set "ARG_MCP_SSE_PORT=%~2"& shift & shift & goto :parse_args )
if "%~1"=="--artifact-root"   ( set "ARG_ARTIFACT_ROOT=%~2"& shift & shift & goto :parse_args )
echo Unknown option: %~1
exit /b 1
:done_args

REM Resolve relative --project path to absolute
if defined ARG_PROJECT (
    pushd "%ORIG_PWD%" 2>nul
    if not exist "!ARG_PROJECT!" mkdir "!ARG_PROJECT!" 2>nul
    pushd "!ARG_PROJECT!" 2>nul && (
        set "ARG_PROJECT=!CD!"
        popd
    ) || (
        echo Error: project path not found: !ARG_PROJECT!
        popd
        exit /b 1
    )
    popd
)

REM If --project was given, resolve instance (ports, dirs)
set "RUN_FLAGS="
set "WRAPPER_FLAGS="
if defined ARG_PROJECT (
    set "RESOLVE_CMD=python scripts\resolve_project_instance.py --project "!ARG_PROJECT!""
    if defined ARG_PROJECT_NAME  set "RESOLVE_CMD=!RESOLVE_CMD! --project-name "!ARG_PROJECT_NAME!""
    if defined ARG_PORT          set "RESOLVE_CMD=!RESOLVE_CMD! --port !ARG_PORT!"
    if defined ARG_MCP_HTTP_PORT set "RESOLVE_CMD=!RESOLVE_CMD! --mcp-http-port !ARG_MCP_HTTP_PORT!"
    if defined ARG_MCP_SSE_PORT  set "RESOLVE_CMD=!RESOLVE_CMD! --mcp-sse-port !ARG_MCP_SSE_PORT!"
    if defined ARG_ARTIFACT_ROOT set "RESOLVE_CMD=!RESOLVE_CMD! --artifact-root "!ARG_ARTIFACT_ROOT!""

    for /f "tokens=1,* delims==" %%a in ('!RESOLVE_CMD!') do set "%%a=%%b"

    if not defined AGENTCHATTR_PROJECT (
        echo Error: resolve_project_instance.py failed.
        exit /b 1
    )

    set "RUN_FLAGS=--project "!AGENTCHATTR_PROJECT!" --project-name "!AGENTCHATTR_PROJECT_NAME!" --project-id "!AGENTCHATTR_PROJECT_ID!" --data-dir "!AGENTCHATTR_DATA_DIR!" --upload-dir "!AGENTCHATTR_UPLOAD_DIR!" --artifact-root "!AGENTCHATTR_ARTIFACT_ROOT!" --port !AGENTCHATTR_PORT! --mcp-http-port !AGENTCHATTR_MCP_HTTP_PORT! --mcp-sse-port !AGENTCHATTR_MCP_SSE_PORT!"
    set "WRAPPER_FLAGS=!RUN_FLAGS!"

    echo agentchattr project: !AGENTCHATTR_PROJECT_ID!
    echo web UI: http://127.0.0.1:!AGENTCHATTR_PORT!/
)

REM Start server if not already running, then wait for it
set "CHECK_PORT=8300"
if defined AGENTCHATTR_PORT set "CHECK_PORT=!AGENTCHATTR_PORT!"
netstat -ano | findstr :!CHECK_PORT! | findstr LISTENING >nul 2>&1
if %errorlevel% neq 0 (
    start "agentchattr server" cmd /c "python run.py !RUN_FLAGS!"
)
:wait_server
netstat -ano | findstr :!CHECK_PORT! | findstr LISTENING >nul 2>&1
if %errorlevel% neq 0 (
    timeout /t 1 /nobreak >nul
    goto :wait_server
)

python wrapper.py claude !WRAPPER_FLAGS! --dangerously-skip-permissions
if %errorlevel% neq 0 (
    echo.
    echo   Agent exited unexpectedly. Check the output above.
    pause
)
