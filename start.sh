#!/usr/bin/env bash
# ============================================================================
#  CodeForge 一键启动脚本  (Linux / macOS / Termux;Windows 用户请用 start.bat)
# ----------------------------------------------------------------------------
#  用法:
#    ./start.sh                       默认 (host=0.0.0.0 port=9191 自动开浏览器)
#    ./start.sh --port 8080           自定义端口
#    ./start.sh --host 127.0.0.1      自定义监听地址
#    ./start.sh --no-browser          不自动开浏览器
#    ./start.sh --rebuild             强制重建 .venv
#    ./start.sh --update              只更新 pip 依赖,不动 venv
#    ./start.sh --dev                 开发模式 (启用 Flask debug)
#    ./start.sh --help                帮助
#
#  Windows 用户(双击即跑):
#    start.bat        纯 cmd 脚本,任何 Windows 都能跑
#    start.ps1        PowerShell 版本(需先 Set-ExecutionPolicy RemoteSigned)
# ============================================================================
set -e

# ----------- 路径定位 (脚本在哪,根目录就在哪) -----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SCRIPT_DIR"

# ----------- 默认值 -----------
HOST="0.0.0.0"
PORT="9191"
OPEN_BROWSER=1
REBUILD=0
UPDATE_ONLY=0
DEV_MODE=0

# ----------- 解析参数 -----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)        PORT="$2";        shift 2 ;;
        --host)        HOST="$2";        shift 2 ;;
        --no-browser)  OPEN_BROWSER=0;   shift   ;;
        --rebuild)     REBUILD=1;        shift   ;;
        --update)      UPDATE_ONLY=1;    shift   ;;
        --dev)         DEV_MODE=1;       shift   ;;
        -h|--help)
            sed -n '2,22p' "$0"
            exit 0
            ;;
        *)
            echo "[start.sh] 未知参数: $1 (试试 --help)" >&2
            exit 1
            ;;
    esac
done

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
echo "[start.sh] platform = $PLATFORM  (cwd: $SCRIPT_DIR)"

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
find_python || { echo "[start.sh] 找不到 python,请先安装 Python 3.10+" >&2; exit 1; }
echo "[start.sh] python  = $PY"

# ----------- venv 路径 -----------
VENV_DIR="$SCRIPT_DIR/.venv"
case "$PLATFORM" in
    windows) ACTIVATE="$VENV_DIR/Scripts/activate"  ;;
    *)       ACTIVATE="$VENV_DIR/bin/activate"      ;;
esac

# ----------- 重建 venv -----------
if [[ $REBUILD -eq 1 && -d "$VENV_DIR" ]]; then
    echo "[start.sh] 重建 venv (--rebuild) ..."
    rm -rf "$VENV_DIR"
fi

# ----------- 创建/激活 venv -----------
if [[ ! -f "$ACTIVATE" ]]; then
    echo "[start.sh] 创建 venv ..."
    "$PY" -m venv "$VENV_DIR" || { echo "[start.sh] venv 创建失败" >&2; exit 1; }
    # 重新定位 venv 里的 python
    case "$PLATFORM" in
        windows) PY="$VENV_DIR/Scripts/python.exe" ;;
        *)       PY="$VENV_DIR/bin/python"         ;;
    esac
fi

# shellcheck disable=SC1090
source "$ACTIVATE"

# ----------- 升级 pip + 装依赖 -----------
if [[ ! -f "$VENV_DIR/.deps_installed" ]] || [[ $UPDATE_ONLY -eq 1 ]]; then
    echo "[start.sh] 安装依赖 ..."
    python -m pip install --upgrade pip wheel --quiet
    if [[ -f "$SCRIPT_DIR/requirement.txt" ]]; then
        python -m pip install -r "$SCRIPT_DIR/requirement.txt" --quiet
    elif [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
        python -m pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
    else
        echo "[start.sh] 警告: 找不到 requirement.txt / requirements.txt" >&2
    fi
    touch "$VENV_DIR/.deps_installed"
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
    echo "[start.sh] 警告: 端口 $PORT 已被占用,试着改 --port" >&2
    read -r -p "    仍然继续吗? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

# ----------- 自动开浏览器(后台) -----------
open_browser() {
    local url="http://localhost:$PORT/"
    sleep 1.2   # 等 Flask 起来
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
            echo "[start.sh] Termux 无桌面浏览器,请手机浏览器打开: $url"
            ;;
    esac
}

# ----------- 启动主程序 -----------
echo
echo "============================================================"
echo "  CodeForge  平台=$PLATFORM  host=$HOST  port=$PORT"
echo "  浏览器:    http://localhost:$PORT/"
echo "  停止:      Ctrl + C"
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
LOG_DIR="$SCRIPT_DIR/log"
mkdir -p "$LOG_DIR"
# 把历史可能落在根目录的 server.log / server.err.log 迁到 log/(首次迁移,不删除,留底)
for name in server.log server.err.log; do
    if [[ -f "$SCRIPT_DIR/$name" && ! -f "$LOG_DIR/$name" ]]; then
        mv "$SCRIPT_DIR/$name" "$LOG_DIR/$name"
        echo "[start.sh] 已迁移 $name -> log/$name"
    fi
done
LOG_OUT="$LOG_DIR/server.log"
LOG_ERR="$LOG_DIR/server.err.log"
echo "[start.sh] stdout -> $LOG_OUT"
echo "[start.sh] stderr -> $LOG_ERR"

# exec 让 main.py 接收 SIGINT 优雅退出;stdout/stderr 各自追加重定向到 log/
exec python "$SCRIPT_DIR/main.py" "${ARGS[@]}" >>"$LOG_OUT" 2>>"$LOG_ERR"
