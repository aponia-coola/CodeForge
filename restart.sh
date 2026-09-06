#!/usr/bin/env bash
# CodeForge 服务控制脚本
#
# 本脚本只负责启动、停止、重启和查看服务状态。
# Python/venv/依赖安装由 deploy_termux.sh 或用户自己的环境管理流程负责。

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
ORIGINAL_ARGS=("$@")

HOST="127.0.0.1"
PORT="9191"
OPEN_BROWSER=1
DEV_MODE=0
ACTION="start"

PID_FILE="$SCRIPT_DIR/log/server.pid"
LOG_DIR="$SCRIPT_DIR/log"
LOG_OUT="$LOG_DIR/server.log"
LOG_ERR="$LOG_DIR/server.err.log"

usage() {
    sed -n '2,9p' "$0"
    cat <<'EOF'

用法：
  ./restart.sh [start|stop|restart|status]
              [--host HOST] [--port PORT] [--no-browser] [--dev]

说明：依赖安装请运行 deploy_termux.sh；本脚本不会执行 pip install。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        start) ACTION="start"; shift ;;
        stop|--stop) ACTION="stop"; shift ;;
        restart|--restart) ACTION="restart"; shift ;;
        status|--status) ACTION="status"; shift ;;
        --host)
            [[ $# -ge 2 ]] || { echo "缺少 --host 参数" >&2; exit 2; }
            HOST="$2"; shift 2 ;;
        --port)
            [[ $# -ge 2 ]] || { echo "缺少 --port 参数" >&2; exit 2; }
            PORT="$2"; shift 2 ;;
        --no-browser) OPEN_BROWSER=0; shift ;;
        --dev) DEV_MODE=1; shift ;;
        --foreground|--console) shift ;;
        --rebuild|--update)
            echo "[restart.sh] $1 已移除：请使用 deploy_termux.sh 管理环境和依赖。" >&2
            exit 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[restart.sh] 未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
done

detect_platform() {
    local uname_s uname_all
    uname_s="$(uname -s 2>/dev/null || true)"
    uname_all="$(uname -a 2>/dev/null || true)"
    case "$uname_s" in
        Linux*)
            if printf '%s\n' "$uname_all" | grep -Eqi '(android|termux)' ||
               [[ -d /data/data/com.termux ]] ||
               [[ "${PREFIX:-}" == *com.termux* ]]; then
                echo termux
            else
                echo linux
            fi ;;
        Darwin*) echo macos ;;
        MINGW*|MSYS*|CYGWIN*) echo windows ;;
        *) echo unknown ;;
    esac
}

PLATFORM="$(detect_platform)"

# 共享存储无法创建 venv 符号链接。若私有目录已有部署，直接转过去，
# 不重新执行 deploy，也不在共享存储创建任何运行文件。
if [[ "$PLATFORM" == termux ]] &&
   printf '%s\n' "$SCRIPT_DIR" | grep -Eqi '(/storage/|/sdcard/)'; then
    PRIVATE_DIR="${HOME}/codeforge"
    if [[ -f "$PRIVATE_DIR/restart.sh" ]]; then
        echo "[restart.sh] 切换到 Termux 私有目录：$PRIVATE_DIR"
        exec bash "$PRIVATE_DIR/restart.sh" "${ORIGINAL_ARGS[@]}"
    fi
    echo "[restart.sh] 当前目录位于 Android 共享存储，无法运行服务。" >&2
    echo "[restart.sh] 首次部署请执行：bash ./deploy_termux.sh" >&2
    exit 1
fi

get_pid() {
    local pid=""
    if [[ -f "$PID_FILE" ]]; then
        pid="$(head -n 1 "$PID_FILE" 2>/dev/null | tr -d '[:space:]')"
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            printf '%s\n' "$pid"
            return 0
        fi
    fi
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti :"$PORT" 2>/dev/null | head -n 1
        return 0
    fi
    if command -v ss >/dev/null 2>&1; then
        ss -lptn "sport = :$PORT" 2>/dev/null |
            grep -o 'pid=[0-9]*' | head -n 1 | cut -d= -f2
    fi
}

show_status() {
    local pid="$(get_pid || true)"
    if [[ -n "$pid" ]]; then
        echo "[restart.sh] 服务运行中，PID=$pid，端口=$PORT"
        return 0
    fi
    echo "[restart.sh] 服务未运行"
    return 1
}

stop_server() {
    local pid waited=0
    pid="$(get_pid || true)"
    if [[ -z "$pid" ]]; then
        rm -f "$PID_FILE"
        echo "[restart.sh] 服务未运行"
        return 0
    fi
    echo "[restart.sh] 停止服务 PID=$pid ..."
    kill "$pid" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null && (( waited < 10 )); do
        sleep 1
        ((waited += 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "[restart.sh] 服务未正常退出，发送 SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
}

if [[ "$ACTION" == status ]]; then
    show_status
    exit $?
fi
if [[ "$ACTION" == stop ]]; then
    stop_server
    exit 0
fi
if [[ "$ACTION" == restart ]]; then
    stop_server
    sleep 1
fi

if [[ ! -f "$SCRIPT_DIR/main.py" ]]; then
    echo "[restart.sh] 找不到 main.py：$SCRIPT_DIR" >&2
    exit 1
fi

case "$PLATFORM" in
    windows) VENV_PY="$SCRIPT_DIR/.venv/Scripts/python.exe" ;;
    *) VENV_PY="$SCRIPT_DIR/.venv/bin/python" ;;
esac

if [[ ! -x "$VENV_PY" ]]; then
    echo "[restart.sh] 找不到可用 venv：$VENV_PY" >&2
    if [[ "$PLATFORM" == termux && -x "$SCRIPT_DIR/deploy_termux.sh" ]]; then
        echo "[restart.sh] 请先执行：bash ./deploy_termux.sh" >&2
    else
        echo "[restart.sh] 请先创建 .venv 并安装 requirements.txt 依赖" >&2
    fi
    exit 1
fi

if ! "$VENV_PY" -c 'import flask, openai' >/dev/null 2>"$SCRIPT_DIR/.runtime_check_err"; then
    echo "[restart.sh] Python 运行依赖不完整，拒绝启动。" >&2
    cat "$SCRIPT_DIR/.runtime_check_err" >&2
    echo "[restart.sh] 请先运行 deploy_termux.sh 或补齐 requirements.txt 依赖。" >&2
    rm -f "$SCRIPT_DIR/.runtime_check_err"
    exit 1
fi
rm -f "$SCRIPT_DIR/.runtime_check_err"

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    echo "[restart.sh] 端口无效：$PORT" >&2
    exit 2
fi

mkdir -p "$LOG_DIR"
if [[ -f "$SCRIPT_DIR/server.log" && ! -f "$LOG_OUT" ]]; then mv "$SCRIPT_DIR/server.log" "$LOG_OUT"; fi
if [[ -f "$SCRIPT_DIR/server.err.log" && ! -f "$LOG_ERR" ]]; then mv "$SCRIPT_DIR/server.err.log" "$LOG_ERR"; fi

if [[ -n "$(get_pid || true)" ]]; then
    echo "[restart.sh] 服务已经在运行：$(get_pid)"
    exit 0
fi

if command -v ss >/dev/null 2>&1 &&
   ss -ltn 2>/dev/null | awk '{print $4}' | grep -E "[:.]$PORT$" >/dev/null 2>&1; then
    echo "[restart.sh] 端口已被占用：$PORT" >&2
    exit 1
fi

if [[ -z "${CODEFORGE_TOKEN:-}" ]]; then
    CODEFORGE_TOKEN="$($VENV_PY -c 'import secrets; print(secrets.token_urlsafe(32))')"
    TOKEN_SOURCE="本次启动随机生成"
else
    TOKEN_SOURCE="环境变量 CODEFORGE_TOKEN"
fi
export CODEFORGE_TOKEN

DISPLAY_HOST="$HOST"
case "$HOST" in
    0.0.0.0|::|"") DISPLAY_HOST=127.0.0.1 ;;
    *:*) DISPLAY_HOST="[$HOST]" ;;
esac
URL="http://$DISPLAY_HOST:$PORT/#token=$CODEFORGE_TOKEN"

echo
echo "============================================================"
echo "  CodeForge"
echo "------------------------------------------------------------"
echo "  平台       : $PLATFORM"
echo "  绑定       : $HOST:$PORT"
echo "  浏览器地址 : $URL"
echo "  Token      : $CODEFORGE_TOKEN"
echo "  Token 来源 : $TOKEN_SOURCE"
echo "  stdout     : $LOG_OUT"
echo "  stderr     : $LOG_ERR"
echo "============================================================"

WAKE_LOCK=0
if [[ "$PLATFORM" == termux ]] && command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock >/dev/null 2>&1 || true
    WAKE_LOCK=1
fi

cleanup() {
    local code=$?
    rm -f "$PID_FILE"
    if (( WAKE_LOCK == 1 )) && command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock >/dev/null 2>&1 || true
    fi
    if (( code != 0 )); then
        echo "[restart.sh] 服务退出，退出码=$code" >&2
        [[ -f "$LOG_ERR" ]] && tail -n 30 "$LOG_ERR" >&2 || true
    fi
}
trap cleanup EXIT INT TERM

MAIN_ARGS=(--host "$HOST" --port "$PORT")
if (( DEV_MODE == 1 )); then MAIN_ARGS+=(--debug); fi

echo "[restart.sh] 启动 main.py ..."
echo "[restart.sh] Ctrl+C 停止服务"
[[ "$PLATFORM" == termux ]] && echo "[restart.sh] Termux wake lock = enabled"
echo

"$VENV_PY" "$SCRIPT_DIR/main.py" "${MAIN_ARGS[@]}" \
    >>"$LOG_OUT" 2>>"$LOG_ERR" &
SERVER_PID=$!
printf '%s\n' "$SERVER_PID" > "$PID_FILE"
wait "$SERVER_PID"
