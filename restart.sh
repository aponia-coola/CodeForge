#!/usr/bin/env bash
# ============================================================================
# CodeForge 一键启动脚本
# Linux / macOS / Termux (use deploy_termux.sh for Termux setup)
# ============================================================================

set -e

# ----------- 路径定位 -----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SCRIPT_DIR"

# ----------- 默认值 -----------
HOST="127.0.0.1"
PORT="9191"
OPEN_BROWSER=1
REBUILD=0
UPDATE_ONLY=0
DEV_MODE=0
NO_SYMLINKS=0
DO_STOP=0
DO_RESTART=0
DO_STATUS=0
ACTION=""
ORIGINAL_ARGS=("$@")

PID_FILE="$SCRIPT_DIR/log/server.pid"
LOG_DIR="$SCRIPT_DIR/log"

# ----------- 参数解析 -----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            PORT="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --no-browser)
            OPEN_BROWSER=0
            shift
            ;;
        --rebuild)
            REBUILD=1
            shift
            ;;
        --update)
            UPDATE_ONLY=1
            shift
            ;;
        --dev)
            DEV_MODE=1
            shift
            ;;
        --no-symlinks)
            NO_SYMLINKS=1
            shift
            ;;
        start)
            ACTION="start"
            shift
            ;;
        stop|--stop)
            DO_STOP=1
            shift
            ;;
        restart|--restart)
            DO_RESTART=1
            shift
            ;;
        status|--status)
            DO_STATUS=1
            shift
            ;;
        --foreground|--console)
            shift
            ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "[restart.sh] 未知参数: $1" >&2
            echo "使用 ./restart.sh --help 查看帮助"
            exit 1
            ;;
    esac
done

# ============================================================================
# PID / 服务管理
# ============================================================================

get_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid="$(head -n 1 "$PID_FILE" 2>/dev/null | tr -d ' \r\n')"

        if [[ "$pid" =~ ^[0-9]+$ ]] &&
           kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
    fi

    # lsof
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti :"$PORT" 2>/dev/null | head -n 1
        return 0
    fi

    # ss
    if command -v ss >/dev/null 2>&1; then
        ss -lptn "sport = :$PORT" 2>/dev/null |
            grep -o 'pid=[0-9]*' |
            head -n 1 |
            cut -d= -f2
        return 0
    fi

    # Python fallback
    python - "$PORT" <<'PY' 2>/dev/null
import os
import re
import sys

port = int(sys.argv[1])
hex_port = "%04X" % port

inodes = set()

try:
    with open("/proc/net/tcp") as f:
        for line in f:
            parts = line.split()

            if len(parts) < 10:
                continue

            local = parts[1]
            state = parts[3]

            if state == "0A" and (
                local.endswith(":" + hex_port) or
                local.endswith(":" + hex_port.lower())
            ):
                inodes.add(parts[9])
except OSError:
    sys.exit(0)

for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue

    fd_dir = "/proc/%s/fd" % pid

    try:
        for fd in os.listdir(fd_dir):
            try:
                target = os.readlink(
                    os.path.join(fd_dir, fd)
                )
            except OSError:
                continue

            match = re.search(
                r"socket:\[(\d+)\]",
                target
            )

            if match and match.group(1) in inodes:
                print(pid)
                sys.exit(0)

    except OSError:
        continue
PY
}

show_status() {
    local pid
    pid="$(get_pid)"

    if [[ -n "$pid" ]]; then
        echo "[restart.sh] 运行中"
        echo "  PID  : $pid"
        echo "  PORT : $PORT"
    else
        echo "[restart.sh] 未运行"
        echo "  PORT : $PORT"
    fi
}

stop_server() {
    local pid
    pid="$(get_pid)"

    if [[ -z "$pid" ]]; then
        echo "[restart.sh] 未发现运行中的服务"
        echo "[restart.sh] port=$PORT"
        return 0
    fi

    echo "[restart.sh] 停止 pid=$pid port=$PORT ..."

    kill "$pid" 2>/dev/null || true

    for i in {1..15}; do
        sleep 0.4

        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "[restart.sh] 进程仍未退出，执行 kill -9 ..."
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"

    pid="$(get_pid)"

    if [[ -z "$pid" ]]; then
        echo "[restart.sh] 已停止"
    else
        echo "[restart.sh] 仍存在进程 $pid"
    fi
}

if [[ $DO_STATUS -eq 1 ]]; then
    show_status
    exit 0
fi

if [[ $DO_STOP -eq 1 && $DO_RESTART -eq 0 ]]; then
    stop_server
    exit 0
fi

if [[ $DO_RESTART -eq 1 ]]; then
    stop_server
    sleep 1
    echo "[restart.sh] 重启中..."
fi

# ============================================================================
# 平台检测
# ============================================================================

detect_platform() {
    local u
    u="$(uname -s 2>/dev/null || echo unknown)"

    case "$u" in
        Linux*)
            if [[ -d "/data/data/com.termux" ]] ||
               [[ "${PREFIX:-}" == *"com.termux"* ]] ||
               uname -o 2>/dev/null | grep -qiE "android"; then
                echo "termux"
            else
                echo "linux"
            fi
            ;;

        Darwin*)
            echo "macos"
            ;;

        MINGW*|MSYS*|CYGWIN*)
            echo "windows"
            ;;

        *)
            echo "unknown"
            ;;
    esac
}

PLATFORM="$(detect_platform)"

echo "[restart.sh] platform = $PLATFORM"
echo "[restart.sh] cwd      = $SCRIPT_DIR"

# Android 共享存储不支持 venv 所需的符号链接。
# Termux 部署应先通过 deploy_termux.sh 迁移到 ~/codeforge。
if [[ "$PLATFORM" == "termux" ]] &&
   pwd | grep -E '(/storage/|/sdcard/)' >/dev/null 2>&1; then
    if [[ -f "$HOME/codeforge/restart.sh" ]]; then
        echo "[restart.sh] 自动切换到 Termux 私有目录：$HOME/codeforge"
        exec bash "$HOME/codeforge/restart.sh" "${ORIGINAL_ARGS[@]}"
    fi
    echo
    echo "[restart.sh] 错误：当前项目位于 Android 共享存储，无法创建 venv"
    echo "[restart.sh] 请先执行："
    echo "  bash ./deploy_termux.sh"
    echo "然后从私有目录启动："
    echo "  cd \"$HOME/codeforge\" && bash ./restart.sh"
    exit 1
fi

# ============================================================================
# Python 检测
# ============================================================================

PY=""

find_python() {

    # 优先使用系统 Python
    for c in python3 python; do

        if command -v "$c" >/dev/null 2>&1; then

            local candidate
            candidate="$(command -v "$c")"

            if "$candidate" --version >/dev/null 2>&1; then
                PY="$candidate"

                echo "[restart.sh] python = $PY"
                "$PY" --version

                return 0
            fi
        fi

    done

    # 常见系统路径
    for p in \
        /data/data/com.termux/files/usr/bin/python3 \
        /data/data/com.termux/files/usr/bin/python \
        /usr/bin/python3 \
        /usr/bin/python; do

        if [[ -x "$p" ]] &&
           "$p" --version >/dev/null 2>&1; then

            PY="$p"

            echo "[restart.sh] python = $PY"
            "$PY" --version

            return 0
        fi

    done

    return 1
}

if ! find_python; then

    echo
    echo "[restart.sh] 错误：找不到 Python"
    echo

    exit 1
fi

# ============================================================================
# Python 版本检查
# ============================================================================

"$PY" - <<'PY'
import sys

if sys.version_info < (3, 10):
    print(
        "[restart.sh] Python 版本过低，需要 Python 3.10+",
        file=sys.stderr
    )
    print(
        f"[restart.sh] 当前版本: {sys.version}",
        file=sys.stderr
    )
    sys.exit(1)

print(
    f"[restart.sh] Python OK: {sys.version.split()[0]}"
)
PY

# ============================================================================
# venv
# ============================================================================

VENV_DIR="$SCRIPT_DIR/.venv"

case "$PLATFORM" in
    windows)
        VENV_PY="$VENV_DIR/Scripts/python.exe"
        ACTIVATE="$VENV_DIR/Scripts/activate"
        ;;
    *)
        VENV_PY="$VENV_DIR/bin/python"
        ACTIVATE="$VENV_DIR/bin/activate"
        ;;
esac

# ============================================================================
# 重建 venv
# ============================================================================

if [[ $REBUILD -eq 1 && -d "$VENV_DIR" ]]; then

    echo "[restart.sh] 删除旧 venv ..."

    rm -rf "$VENV_DIR"
fi

# ============================================================================
# 创建 venv
# ============================================================================

venv_ok=0

if [[ -x "$VENV_PY" ]]; then

    if "$VENV_PY" --version >/dev/null 2>&1; then
        venv_ok=1
    fi

fi

if [[ $venv_ok -eq 0 ]]; then

    if [[ -d "$VENV_DIR" ]]; then
        rm -rf "$VENV_DIR"
    fi

    echo "[restart.sh] 创建 Python venv ..."

    if ! "$PY" -m venv "$VENV_DIR" 2>"$SCRIPT_DIR/.venv_err"; then
        echo "[restart.sh] venv 默认模式失败，自动切换 --copies ..."
        rm -rf "$VENV_DIR"
        "$PY" -m venv --copies "$VENV_DIR" || {
            echo "[restart.sh] venv 创建失败"
            echo "[restart.sh] 错误日志：$SCRIPT_DIR/.venv_err"
            exit 1
        }
    fi
fi

PY="$VENV_PY"

if [[ ! -x "$PY" ]]; then
    echo "[restart.sh] venv Python 不存在：$PY"
    exit 1
fi

echo "[restart.sh] venv python = $PY"

# ============================================================================
# 激活
# ============================================================================

if [[ -f "$ACTIVATE" ]]; then
    # shellcheck disable=SC1090
    source "$ACTIVATE"
fi

# ============================================================================
# pip 初始化
# ============================================================================

echo
echo "[restart.sh] 检查 pip ..."

if ! "$PY" -m pip --version >/dev/null 2>&1; then

    echo "[restart.sh] pip 不存在，尝试 ensurepip ..."

    "$PY" -m ensurepip --upgrade || true
fi

if ! "$PY" -m pip --version >/dev/null 2>&1; then

    echo
    echo "[restart.sh] 错误：无法获得 pip"
    exit 1
fi

echo "[restart.sh] pip = OK"

# ============================================================================
# requirements 定位
# ============================================================================

REQ_FILE=""

for name in \
    requirements.txt \
    requirement.txt; do

    if [[ -f "$SCRIPT_DIR/$name" ]]; then
        REQ_FILE="$SCRIPT_DIR/$name"
        break
    fi

done

if [[ -z "$REQ_FILE" ]]; then

    echo
    echo "[restart.sh] 警告：没有找到 requirements.txt"
    echo "[restart.sh] 跳过依赖安装"
    echo

else

    echo
    echo "[restart.sh] requirements = $(basename "$REQ_FILE")"

fi

# ============================================================================
# requirements hash
# ============================================================================

DEPS_STAMP="$VENV_DIR/.deps_installed"

req_hash() {

    if [[ -z "$REQ_FILE" ]]; then
        echo "no-requirements"
        return 0
    fi

    "$PY" - "$REQ_FILE" <<'PY'
import hashlib
import sys

with open(sys.argv[1], "rb") as f:
    print(hashlib.sha256(f.read()).hexdigest())
PY
}

WANT_HASH="$(req_hash)"

HAVE_HASH=""

if [[ -f "$DEPS_STAMP" ]]; then
    HAVE_HASH="$(
        head -n 1 "$DEPS_STAMP" 2>/dev/null || true
    )"
fi

# ============================================================================
# 依赖安装
#
# 第一方案：
#   直接 pip install -r requirements.txt
#
# 第二方案：
#   第一方案失败后：
#   - 逐个解析 requirements
#   - 每个依赖独立 pip install
#   - 单个失败不会阻塞其它依赖
#
# Termux 的特殊依赖策略由 deploy_termux.sh 负责。
# ============================================================================

install_dependencies_fallback() {

    echo
    echo "============================================================"
    echo "[restart.sh] 第二方案：逐项安装 Python 依赖"
    echo "============================================================"
    echo

    local total=0
    local success=0
    local failed=0

    while IFS= read -r line || [[ -n "$line" ]]; do

        # 去除 CR
        line="${line%$'\r'}"

        # 去除前后空格
        line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

        # 跳过空行
        [[ -z "$line" ]] && continue

        # 跳过注释
        [[ "$line" == \#* ]] && continue

        # 跳过 pip 参数
        case "$line" in
            -r\ *|-e\ *|--*)
                echo "[restart.sh] 跳过特殊 requirements 行：$line"
                continue
                ;;
        esac

        total=$((total + 1))

        echo
        echo "------------------------------------------------------------"
        echo "[restart.sh] [$total] 安装：$line"
        echo "------------------------------------------------------------"

        if "$PY" -m pip install "$line"; then

            success=$((success + 1))

            echo "[restart.sh] ✓ 安装成功：$line"

        else

            failed=$((failed + 1))

            echo "[restart.sh] ✗ 安装失败：$line"
            echo "[restart.sh]    继续安装其它依赖..."

        fi

    done < "$REQ_FILE"

    echo
    echo "============================================================"
    echo "[restart.sh] 第二方案安装完成"
    echo "  总依赖 : $total"
    echo "  成功    : $success"
    echo "  失败    : $failed"
    echo "============================================================"
    echo

    # 第二方案即使存在失败，也继续启动
    return 0
}

# ============================================================================
# 安装依赖
# ============================================================================

if [[ -n "$REQ_FILE" ]]; then

    if [[ "$WANT_HASH" != "$HAVE_HASH" ]] ||
       [[ $UPDATE_ONLY -eq 1 ]]; then

        echo
        echo "============================================================"
        echo "[restart.sh] 依赖发生变化，开始安装"
        echo "============================================================"
        echo

        # --------------------------------------------------------------------
        # pip 基础升级
        # --------------------------------------------------------------------

        echo "[restart.sh] 升级 pip / wheel ..."

        "$PY" -m pip install --upgrade pip wheel || {
            echo "[restart.sh] pip/wheel 升级失败，继续尝试安装依赖..."
        }

        # --------------------------------------------------------------------
        # 第一方案
        # --------------------------------------------------------------------

        echo
        echo "============================================================"
        echo "[restart.sh] 第一方案"
        echo "[restart.sh] pip install -r $(basename "$REQ_FILE")"
        echo "============================================================"
        echo

        if "$PY" -m pip install -r "$REQ_FILE"; then

            echo
            echo "[restart.sh] ✓ 第一方案安装成功"
            echo

        else

            echo
            echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
            echo "[restart.sh] 第一方案失败"
            echo "[restart.sh] 开始执行第二方案..."
            echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"

            install_dependencies_fallback
        fi

        # 无论第一方案还是第二方案执行完成，都更新 hash。
        # 下次启动如果 requirements 没变，就不会重复安装。
        printf '%s\n' "$WANT_HASH" > "$DEPS_STAMP"

    else

        echo
        echo "[restart.sh] 依赖未发生变化，跳过 pip install"
        echo

    fi

fi

# ============================================================================
# 端口检查
# ============================================================================

check_port() {

    local p="$1"

    if command -v ss >/dev/null 2>&1; then

        if ss -ltn 2>/dev/null |
            awk '{print $4}' |
            grep -E "[:.]$p$" >/dev/null 2>&1; then
            return 0
        fi

    elif command -v netstat >/dev/null 2>&1; then

        if netstat -ltn 2>/dev/null |
            awk '{print $4}' |
            grep -E "[:.]$p$" >/dev/null 2>&1; then
            return 0
        fi

    fi

    # Python fallback
    if "$PY" - "$p" <<'PY' >/dev/null 2>&1
import socket
import sys

port = int(sys.argv[1])

s = socket.socket()

try:
    s.bind(("0.0.0.0", port))
except OSError:
    sys.exit(0)
else:
    sys.exit(1)
finally:
    s.close()
PY
    then
        return 0
    fi

    return 1
}

if check_port "$PORT"; then

    echo
    echo "[restart.sh] 警告：端口 $PORT 已被占用"
    echo

    read -r -p "[restart.sh] 仍然继续吗? [y/N] " ans

    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

# ============================================================================
# Token
# ============================================================================

if [[ -z "${CODEFORGE_TOKEN:-}" ]]; then

    CODEFORGE_TOKEN="$(
        "$PY" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
    )"

    TOKEN_SOURCE="本次启动随机生成"

else

    TOKEN_SOURCE="环境变量 CODEFORGE_TOKEN"

fi

export CODEFORGE_TOKEN

# ============================================================================
# URL
# ============================================================================

display_host() {

    case "$1" in
        0.0.0.0|::|"")
            echo "127.0.0.1"
            ;;
        *:*)
            echo "[$1]"
            ;;
        *)
            echo "$1"
            ;;
    esac
}

is_loopback() {

    case "$1" in
        127.*|::1|localhost)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

DISPLAY_HOST="$(display_host "$HOST")"

URL="http://$DISPLAY_HOST:$PORT/#token=$CODEFORGE_TOKEN"

# ============================================================================
# 浏览器
# ============================================================================

open_browser() {

    local url="$URL"

    sleep 1.2

    case "$PLATFORM" in

        linux)

            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "$url" >/dev/null 2>&1 || true
            elif command -v sensible-browser >/dev/null 2>&1; then
                sensible-browser "$url" >/dev/null 2>&1 || true
            fi

            ;;

        macos)

            if command -v open >/dev/null 2>&1; then
                open "$url" >/dev/null 2>&1 || true
            fi

            ;;

        windows)

            if command -v start >/dev/null 2>&1; then
                start "$url" >/dev/null 2>&1 || true
            elif command -v cmd.exe >/dev/null 2>&1; then
                cmd.exe /c start "" "$url" >/dev/null 2>&1 || true
            fi

            ;;

        termux)

            echo
            echo "[restart.sh] Termux：请在手机浏览器打开："
            echo
            echo "  $url"
            echo

            ;;
    esac
}

# ============================================================================
# 日志
# ============================================================================

mkdir -p "$LOG_DIR"

# 迁移旧日志
for name in server.log server.err.log; do

    if [[ -f "$SCRIPT_DIR/$name" ]] &&
       [[ ! -f "$LOG_DIR/$name" ]]; then

        mv "$SCRIPT_DIR/$name" "$LOG_DIR/$name"

        echo "[restart.sh] 已迁移 $name -> log/$name"
    fi

done

LOG_OUT="$LOG_DIR/server.log"
LOG_ERR="$LOG_DIR/server.err.log"

# ============================================================================
# 启动信息
# ============================================================================

echo
echo "============================================================"
echo "  CodeForge"
echo "------------------------------------------------------------"
echo "  平台       : $PLATFORM"
echo "  绑定       : $HOST:$PORT"
echo "  浏览器地址 : $URL"
echo "  Token      : $CODEFORGE_TOKEN"
echo "  Token 来源 : $TOKEN_SOURCE"
echo "------------------------------------------------------------"
echo "  stdout     : $LOG_OUT"
echo "  stderr     : $LOG_ERR"
echo "============================================================"

if ! is_loopback "$HOST"; then

    echo
    echo "[!] 警告：当前监听非回环地址 $HOST"
    echo "[!] 局域网其它设备可能可以访问此服务"
    echo "[!] 请确保网络环境可信"
    echo
fi

# ============================================================================
# 自动打开浏览器
# ============================================================================

if [[ $OPEN_BROWSER -eq 1 ]]; then
    open_browser &
fi

# ============================================================================
# main.py 参数
# ============================================================================

ARGS=(
    --host "$HOST"
    --port "$PORT"
)

if [[ $DEV_MODE -eq 1 ]]; then
    ARGS+=(--debug)
fi

# ============================================================================
# PID
# ============================================================================

echo "$$" > "$PID_FILE"

trap '
    rm -f "$PID_FILE" 2>/dev/null || true
    if [[ "$PLATFORM" == "termux" ]] && command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock >/dev/null 2>&1 || true
    fi
' EXIT INT TERM

if [[ "$PLATFORM" == "termux" ]] && command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock >/dev/null 2>&1 || true
    echo "[restart.sh] Termux wake lock = enabled"
fi

# ============================================================================
# 启动 CodeForge
# ============================================================================

echo
echo "[restart.sh] 启动 main.py ..."
echo "[restart.sh] Ctrl+C 停止服务"
echo

exec "$PY" "$SCRIPT_DIR/main.py" \
    "${ARGS[@]}" \
    >>"$LOG_OUT" \
    2>>"$LOG_ERR"
