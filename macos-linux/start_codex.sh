#!/usr/bin/env sh
# agentchattr - starts server (if not running) + Codex wrapper
ORIG_PWD="$PWD"
cd "$(dirname "$0")/.."

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Python 3 is required but was not found on PATH."
    exit 1
fi

ensure_venv() {
    if [ -d ".venv" ] && [ ! -x ".venv/bin/python" ]; then
        echo "Recreating .venv for this platform..."
        rm -rf .venv
    fi

    if [ ! -x ".venv/bin/python" ]; then
        echo "Creating virtual environment..."
        "$PYTHON_BIN" -m venv .venv || {
            echo "Error: failed to create .venv with $PYTHON_BIN."
            exit 1
        }
        .venv/bin/python -m pip install -q -r requirements.txt || {
            echo "Error: failed to install Python dependencies."
            exit 1
        }
    fi
}

is_server_running() {
    port="${AGENTCHATTR_PORT:-8300}"
    lsof -i ":$port" -sTCP:LISTEN >/dev/null 2>&1 || \
    ss -tlnp 2>/dev/null | grep -q ":$port "
}

# --- Parse project flags ---
ARG_PROJECT=""
ARG_PROJECT_NAME=""
ARG_PORT=""
ARG_MCP_HTTP_PORT=""
ARG_MCP_SSE_PORT=""
ARG_ARTIFACT_ROOT=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --project=*)       ARG_PROJECT="${1#*=}"; shift ;;
        --project)         ARG_PROJECT="$2"; shift 2 ;;
        --project-name=*)  ARG_PROJECT_NAME="${1#*=}"; shift ;;
        --project-name)    ARG_PROJECT_NAME="$2"; shift 2 ;;
        --port=*)          ARG_PORT="${1#*=}"; shift ;;
        --port)            ARG_PORT="$2"; shift 2 ;;
        --mcp-http-port=*) ARG_MCP_HTTP_PORT="${1#*=}"; shift ;;
        --mcp-http-port)   ARG_MCP_HTTP_PORT="$2"; shift 2 ;;
        --mcp-sse-port=*)  ARG_MCP_SSE_PORT="${1#*=}"; shift ;;
        --mcp-sse-port)    ARG_MCP_SSE_PORT="$2"; shift 2 ;;
        --artifact-root=*) ARG_ARTIFACT_ROOT="${1#*=}"; shift ;;
        --artifact-root)   ARG_ARTIFACT_ROOT="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Resolve relative --project path to absolute
if [ -n "$ARG_PROJECT" ]; then
    case "$ARG_PROJECT" in
        /*) ;;
        *)  ARG_PROJECT="$ORIG_PWD/$ARG_PROJECT" ;;
    esac
fi

ensure_venv

# If --project was given, resolve instance (ports, dirs)
SERVER_CMD=".venv/bin/python run.py"
if [ -n "$ARG_PROJECT" ]; then
    set -- --project "$ARG_PROJECT"
    [ -n "$ARG_PROJECT_NAME" ]  && set -- "$@" --project-name  "$ARG_PROJECT_NAME"
    [ -n "$ARG_PORT" ]          && set -- "$@" --port          "$ARG_PORT"
    [ -n "$ARG_MCP_HTTP_PORT" ] && set -- "$@" --mcp-http-port "$ARG_MCP_HTTP_PORT"
    [ -n "$ARG_MCP_SSE_PORT" ]  && set -- "$@" --mcp-sse-port  "$ARG_MCP_SSE_PORT"
    [ -n "$ARG_ARTIFACT_ROOT" ] && set -- "$@" --artifact-root "$ARG_ARTIFACT_ROOT"

    RESOLVE_OUT=$(.venv/bin/python scripts/resolve_project_instance.py "$@") || {
        echo "Error: resolve_project_instance.py failed."
        exit 1
    }

    while IFS='=' read -r k v; do
        export "$k=$v"
    done <<EOF
$RESOLVE_OUT
EOF

    SERVER_CMD="$SERVER_CMD --project '${AGENTCHATTR_PROJECT}' --project-name '${AGENTCHATTR_PROJECT_NAME}' --project-id '${AGENTCHATTR_PROJECT_ID}' --data-dir '${AGENTCHATTR_DATA_DIR}' --upload-dir '${AGENTCHATTR_UPLOAD_DIR}' --artifact-root '${AGENTCHATTR_ARTIFACT_ROOT}' --port ${AGENTCHATTR_PORT} --mcp-http-port ${AGENTCHATTR_MCP_HTTP_PORT} --mcp-sse-port ${AGENTCHATTR_MCP_SSE_PORT}"

    echo "agentchattr project: $AGENTCHATTR_PROJECT_ID"
    echo "web UI: http://127.0.0.1:${AGENTCHATTR_PORT}/"
fi

if ! is_server_running; then
    if [ "$(uname -s)" = "Darwin" ]; then
        osascript -e "tell app \"Terminal\" to do script \"cd '$(pwd)' && $SERVER_CMD\"" > /dev/null 2>&1
    else
        if command -v gnome-terminal >/dev/null 2>&1; then
            gnome-terminal -- sh -c "cd '$(pwd)' && $SERVER_CMD; printf 'Press Enter to close... '; read _"
        elif command -v xterm >/dev/null 2>&1; then
            xterm -e sh -c "cd '$(pwd)' && $SERVER_CMD" &
        else
            eval "$SERVER_CMD" > "${AGENTCHATTR_DATA_DIR:-data}/server.log" 2>&1 &
        fi
    fi

    i=0
    while [ "$i" -lt 30 ]; do
        if is_server_running; then
            break
        fi
        sleep 0.5
        i=$((i + 1))
    done
fi

if [ -n "$ARG_PROJECT" ]; then
    .venv/bin/python wrapper.py codex \
        --project "$AGENTCHATTR_PROJECT" \
        --project-name "$AGENTCHATTR_PROJECT_NAME" \
        --project-id "$AGENTCHATTR_PROJECT_ID" \
        --data-dir "$AGENTCHATTR_DATA_DIR" \
        --upload-dir "$AGENTCHATTR_UPLOAD_DIR" \
        --artifact-root "$AGENTCHATTR_ARTIFACT_ROOT" \
        --port "$AGENTCHATTR_PORT" \
        --mcp-http-port "$AGENTCHATTR_MCP_HTTP_PORT" \
        --mcp-sse-port "$AGENTCHATTR_MCP_SSE_PORT"
else
    .venv/bin/python wrapper.py codex
fi
