#!/usr/bin/env bash
# ============================================================================
#  CodeForge 一键启动脚本  (Linux / macOS / Termux;Windows 用户请用 start.ps1)
# ----------------------------------------------------------------------------
#  用法:
#    ./restart.sh                       默认 (host=127.0.0.1 port=9191 自动开浏览器，前台挂起)
#    ./restart.sh --port 8080           自定义端口
#    ./restart.sh --host 0.0.0.0        监听所有网卡(局域网可见,会打印风险提示)
#    ./restart.sh --no-browser          不自动开浏览器
#    ./restart.sh --rebuild             强制重建 .venv
#    ./restart.sh --update              只更新 pip 依赖,不动 venv
#    ./restart.sh --dev                 开发模式 (启用 Flask debug)
#    ./restart.sh start                 启动并前台挂起（默认，Ctrl+C 停止）
#    ./restart.sh stop                  停止占用端口的服务
#    ./restart.sh restart               重启
#    ./restart.sh status                查看是否运行
#    ./restart.sh --help                帮助
#
#  认证:
#    脚本会生成 CODEFORGE_TOKEN 并透传给 main.py,自动打开的 URL 里已带 #token=。
#    想固定 token 就在外面先 export CODEFORGE_TOKEN=...,脚本会沿用不再生成。
#
#  Windows 用户:
#    start.ps1        PowerShell 版本(需先 Set-ExecutionPolicy RemoteSigned)
# ============================================================================
set -e

# ----------- 路径定位 (脚本在哪,根目录就在哪) -----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SCRIPT_DIR"

# ----------- 默认值 -----------
HOST="127.0.0.1"
PORT="9191"
OPEN_BROWSER=1
REBUILD=0
UPDATE_ONLY=0
DEV_MODE=0
DO_STOP=0
DO_RESTART=0
DO_STATUS=0
ACTION=""
PID_FILE="$SCRIPT_DIR/log/server.pid"
LOG_DIR="$SCRIPT_DIR/log"

# ----------- 解析参数 -----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)        PORT="$2";        shift 2 ;;
        --host)        HOST="$2";        shift 2 ;;
        --no-browser)  OPEN_BROWSER=0;   shift   ;;
        --rebuild)     REBUILD=1;        shift   ;;
        --update)      UPDATE_ONLY=1;    shift   ;;
        --dev)         DEV_MODE=1;       shift   ;;
        start)         ACTION="start";   shift   ;;
        stop|--stop)   DO_STOP=1;        shift   ;;
        restart|--restart) DO_RESTART=1; shift   ;;
        status|--status) DO_STATUS=1;    shift   ;;
        --foreground|--console) DEV_MODE="$DEV_MODE"; shift ;; # 兼容前台
        -h|--help)
            sed -n '2,27p' "$0"
            exit 0
            ;;
        *)
            echo "[restart.sh] 未知参数: $1 (试试 --help)" >&2
            exit 1
            ;;
    esac
done
# 兼容位置参数 restart/stop/status 优先
PID_FILE="$SCRIPT_DIR/log/server.pid"
LOG_DIR="$SCRIPT_DIR/log"
get_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local pid; pid="$(head -n 1 "$PID_FILE" 2>/dev/null | tr -d ' \r\n')"
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then echo "$pid"; return 0; fi
    fi
    # 回退：按端口找
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti :"$PORT" 2>/dev/null | head -n 1
    elif command -v ss >/dev/null 2>&1; then
        ss -lptn "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2
    fi
}
show_status() {
    local pid; pid="$(get_pid)"
    if [[ -n "$pid" ]]; then echo "[restart.sh] 运行中  pid=$pid  port=$PORT"; else echo "[restart.sh] 未运行  port=$PORT"; fi
}
stop_server() {
    local pid; pid="$(get_pid)"
    if [[ -z "$pid" ]]; then echo "[restart.sh] 未发现运行中的服务 (port $PORT)"; return 0; fi
    echo "[restart.sh] 停止  pid=$pid  port=$PORT ..."
    kill "$pid" 2>/dev/null || true
    for i in {1..15}; do sleep 0.4; if ! kill -0 "$pid" 2>/dev/null; then break; fi; done
    if kill -0 "$pid" 2>/dev/null; then echo "[restart.sh] 进程 $pid 仍在，尝试 kill -9"; kill -9 "$pid" 2>/dev/null || true; fi
    rm -f "$PID_FILE"
    pid="$(get_pid)"; if [[ -z "$pid" ]]; then echo "[restart.sh] 已停止"; else echo "[restart.sh] 仍有进程 $pid"; fi
}
if [[ $DO_STATUS -eq 1 ]]; then show_status; exit 0; fi
if [[ $DO_STOP -eq 1 && $DO_RESTART -eq 0 ]]; then stop_server; exit 0; fi
if [[ $DO_RESTART -eq 1 ]]; then stop_server; sleep 1; echo "[restart.sh] 重启中..."; fi

# ----------- 平台检测 -----------
detect_platform() {
    local u
    u="$(uname -s 2>/dev/null || echo Windows)"
    case "$u" in
        Linux*)
            # Termux 在 Android 上 /data/data/com.termux/... PATH 里
            if [[ -d "/data/data/com.termux" ]] || [[ "$PREFIX" == *"com.termux"* ]]; then
                echo "termux"
            else
                echo "linux"
            fi
            ;;
        Darwin*)  echo "macos"   ;;
        MINGW*|MSYS*|CYGWIN*)  echo "windows"  ;;
        *)        echo "unknown" ;;
    esac
}
PLATFORM="$(detect_platform)"
echo "[restart.sh] platform = $PLATFORM  (cwd: $SCRIPT_DIR)"

# ----------- Python 解释器选择 -----------
PY=""
find_python() {
    # 优先 .venv 里的(重建时跳过)
    if [[ -z "$PY" && $REBUILD -eq 0 ]]; then
        case "$PLATFORM" in
            windows) [[ -x "$SCRIPT_DIR/.venv/Scripts/python.exe" ]] && PY="$SCRIPT_DIR/.venv/Scripts/python.exe" ;;
            *)       [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]       && PY="$SCRIPT_DIR/.venv/bin/python"       ;;
        esac
    fi
    # 再查系统 PATH
    if [[ -z "$PY" ]]; then
        for c in python3.12 python3.11 python3.10 python3 python; do
            if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; return 0; fi
        done
    fi
    return 1
}
find_python || { echo "[restart.sh] 找不到 python,请先安装 Python 3.10+" >&2; exit 1; }
echo "[restart.sh] python  = $PY"

# ----------- venv 路径 -----------
VENV_DIR="$SCRIPT_DIR/.venv"
case "$PLATFORM" in
    windows) ACTIVATE="$VENV_DIR/Scripts/activate"  ;;
    *)       ACTIVATE="$VENV_DIR/bin/activate"      ;;
esac

# ----------- 重建 venv -----------
if [[ $REBUILD -eq 1 && -d "$VENV_DIR" ]]; then
    echo "[restart.sh] 重建 venv (--rebuild) ..."
    rm -rf "$VENV_DIR"
fi

# ----------- 创建/激活 venv -----------
if [[ ! -f "$ACTIVATE" ]]; then
    echo "[restart.sh] 创建 venv ..."
    "$PY" -m venv "$VENV_DIR" || { echo "[restart.sh] venv 创建失败" >&2; exit 1; }
    # 重新定位 venv 里的 python
    case "$PLATFORM" in
        windows) PY="$VENV_DIR/Scripts/python.exe" ;;
        *)       PY="$VENV_DIR/bin/python"         ;;
    esac
fi

# shellcheck disable=SC1090
source "$ACTIVATE"

# ----------- 依赖清单定位 -----------
# requirements.txt 是正名,requirement.txt 是历史拼写,留着让老 checkout 也能跑
REQ_FILE=""
for name in requirements.txt requirement.txt; do
    if [[ -f "$SCRIPT_DIR/$name" ]]; then REQ_FILE="$SCRIPT_DIR/$name"; break; fi
done

# ----------- 升级 pip + 装依赖 -----------
# 哨兵文件里存的是依赖清单的 sha256,而不是一个空的 touch 标记:
# 清单内容一变,哨兵就对不上,自动重装,不用再手动 --update
DEPS_STAMP="$VENV_DIR/.deps_installed"

req_hash() {
    if [[ -z "$REQ_FILE" ]]; then echo "no-requirements"; return 0; fi
    python - "$REQ_FILE" <<'PY'
import hashlib
import sys
with open(sys.argv[1], 'rb') as f:
    print(hashlib.sha256(f.read()).hexdigest())
PY
}

WANT_HASH="$(req_hash)"
HAVE_HASH=""
[[ -f "$DEPS_STAMP" ]] && HAVE_HASH="$(head -n 1 "$DEPS_STAMP" 2>/dev/null || true)"

if [[ "$WANT_HASH" != "$HAVE_HASH" ]] || [[ $UPDATE_ONLY -eq 1 ]]; then
    if [[ -z "$REQ_FILE" ]]; then
        echo "[restart.sh] 警告: 找不到 requirements.txt / requirement.txt" >&2
    else
        echo "[restart.sh] 安装依赖 ($(basename "$REQ_FILE")) ..."
        python -m pip install --upgrade pip wheel --quiet
        python -m pip install -r "$REQ_FILE" --quiet
    fi
    printf '%s\n' "$WANT_HASH" > "$DEPS_STAMP"
else
    echo "[restart.sh] 依赖未变动,跳过安装"
fi

# ----------- 健康检查(端口占用) -----------
check_port() {
    local p="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -E "[:.]$p\$" >/dev/null 2>&1 && return 0
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -E "[:.]$p\$" >/dev/null 2>&1 && return 0
    fi
    # Termux 没 ss/netstat 时:尝试 python 检测
    python - <<PY 2>/dev/null && return 0
import socket
s = socket.socket()
s.settimeout(0.3)
try:
    s.bind(("0.0.0.0", $p))
except OSError:
    raise SystemExit(0)
PY
    return 1
}
if check_port "$PORT"; then
    echo "[restart.sh] 警告: 端口 $PORT 已被占用,试着改 --port" >&2
    read -r -p "    仍然继续吗? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

# ----------- 认证 token -----------
# auth.token() 优先读 CODEFORGE_TOKEN,没有才随机生成。这里先生成好再导出,
# 脚本打印的 URL 和 main.py 启动横幅里的 token 就一定是同一个
if [[ -z "${CODEFORGE_TOKEN:-}" ]]; then
    CODEFORGE_TOKEN="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
    TOKEN_SOURCE="本次启动随机生成"
else
    TOKEN_SOURCE="环境变量 CODEFORGE_TOKEN"
fi
export CODEFORGE_TOKEN

# ----------- 访问地址 -----------
# 监听地址不等于可访问地址:0.0.0.0 / :: 是通配绑定,浏览器要用回环地址访问。
# 这里和 auth.display_host() 保持同一套映射,横幅才不会和实际绑定对不上
display_host() {
    case "$1" in
        0.0.0.0|::|"") echo "127.0.0.1" ;;
        *:*)           echo "[$1]"      ;;
        *)             echo "$1"        ;;
    esac
}

is_loopback() {
    case "$1" in
        127.*|::1|localhost) return 0 ;;
        *)                   return 1 ;;
    esac
}

DISPLAY_HOST="$(display_host "$HOST")"
URL="http://$DISPLAY_HOST:$PORT/#token=$CODEFORGE_TOKEN"

# ----------- 自动开浏览器(后台) -----------
open_browser() {
    local url="$URL"
    sleep 1.2
    case "$PLATFORM" in
        linux)
            (command -v xdg-open >/dev/null && xdg-open "$url" >/dev/null 2>&1) || \
            (command -v sensible-browser >/dev/null && sensible-browser "$url" >/dev/null 2>&1) || true
            ;;
        macos)
            (command -v open >/dev/null && open "$url" >/dev/null 2>&1) || true
            ;;
        windows)
            (command -v start >/dev/null && start "$url" >/dev/null 2>&1) || \
            (command -v cmd.exe >/dev/null && cmd.exe /c start "" "$url" >/dev/null 2>&1) || true
            ;;
        termux)
            # Termux 没桌面浏览器,提示用户
            echo "[restart.sh] Termux 无桌面浏览器,请手机浏览器打开: $url"
            ;;
    esac
}

# ----------- 启动主程序 -----------
echo
echo "============================================================"
echo "  CodeForge  平台=$PLATFORM  绑定=$HOST:$PORT"
echo "  浏览器:    $URL"
echo "  token :    $CODEFORGE_TOKEN"
echo "  来源  :    $TOKEN_SOURCE"
echo "  Stop   :    Ctrl + C  or ./restart.sh stop in another terminal"
if ! is_loopback "$HOST"; then
    echo "------------------------------------------------------------"
    echo "  [!] 正在绑定非回环地址 $HOST,局域网内任意设备都能连到本服务。"
    echo "  [!] token 是此时唯一的防线,泄露即等同于交出这台机器的 shell。"
    echo "  [!] 只在受信任的网络里这么做,否则请用默认的 --host 127.0.0.1。"
fi
echo "============================================================"
echo

# 后台开浏览器(不阻塞主进程)
if [[ $OPEN_BROWSER -eq 1 ]]; then
    open_browser &
fi

# 透传参数到 main.py
ARGS=(--host "$HOST" --port "$PORT")
[[ $DEV_MODE -eq 1 ]] && ARGS+=(--debug)

# ----------- 日志:统一输出到 log/ 目录 -----------
mkdir -p "$LOG_DIR"
# 把历史可能落在根目录的 server.log / server.err.log 迁到 log/(首次迁移,不删除,留底)
for name in server.log server.err.log; do
    if [[ -f "$SCRIPT_DIR/$name" && ! -f "$LOG_DIR/$name" ]]; then
        mv "$SCRIPT_DIR/$name" "$LOG_DIR/$name"
        echo "[restart.sh] 已迁移 $name -> log/$name"
    fi
done
LOG_OUT="$LOG_DIR/server.log"
LOG_ERR="$LOG_DIR/server.err.log"
echo "[restart.sh] stdout -> $LOG_OUT"
echo "[restart.sh] stderr -> $LOG_ERR"
echo "[restart.sh] foreground hanging, Ctrl+C to stop (or ./restart.sh stop in another terminal)"
echo "$PID" > "$PID_FILE" 2>/dev/null || true
# 退出时清 pid（exec 后 PID 不变，stop 按此文件杀）
trap 'rm -f "$PID_FILE" 2>/dev/null' EXIT INT TERM

# exec 让 main.py 接收 SIGINT 优雅退出;stdout/stderr 各自追加重定向到 log/
exec python "$SCRIPT_DIR/main.py" "${ARGS[@]}" >>"$LOG_OUT" 2>>"$LOG_ERR"
