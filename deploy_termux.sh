#!/usr/bin/env bash
# CodeForge Termux 部署脚本
# 负责 Termux 的系统依赖、共享存储迁移、venv 和 Python 依赖安装。

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { printf '[deploy_termux.sh] %s\n' "$*"; }
die() { printf '[deploy_termux.sh] 错误：%s\n' "$*" >&2; exit 1; }

VENV_ERR=""
FILTERED_REQ=""
cleanup() {
    [[ -z "$VENV_ERR" ]] || rm -f "$VENV_ERR"
    [[ -z "$FILTERED_REQ" ]] || rm -f "$FILTERED_REQ"
}
trap cleanup EXIT

UNAME_ALL="$(uname -a 2>/dev/null || true)"
UNAME_OS="$(uname -o 2>/dev/null || true)"
printf '%s\n%s\n' "$UNAME_ALL" "$UNAME_OS" |
    grep -Eqi '(termux|android)' || die "此脚本只能在 Termux 中运行"

# Android 共享存储经常不支持符号链接和完整的 Unix 权限。
if pwd | grep -E '(/storage/|/sdcard/)' >/dev/null 2>&1; then
    DEST="${HOME}/codeforge"
    [[ "$SCRIPT_DIR" != "$DEST" ]] || die "目标目录不能与当前目录相同"
    log "检测到共享存储：$SCRIPT_DIR"
    log "迁移项目到：$DEST"
    mkdir -p "$DEST"
    # .git 不参与运行，且其中的对象权限经常导致跨存储复制失败。
    # .venv 也不跨目录复用，避免带入共享存储上的符号链接。
    shopt -s dotglob nullglob
    for item in "$SCRIPT_DIR"/*; do
        name="$(basename "$item")"
        case "$name" in
            .git|.venv) continue ;;
        esac
        cp -r "$item" "$DEST/" || die "项目迁移失败：$name，请检查存储权限"
    done
    cd "$DEST"
    exec bash "$DEST/deploy_termux.sh" "$@"
fi

command -v pkg >/dev/null 2>&1 || die "找不到 pkg，请确认正在 Termux 中运行"
log "安装/更新 Termux Python"
pkg update -y
pkg install -y python curl

# 这些是可选的 Termux 原生包，不同镜像/仓库的包名和可用性可能不同。
# 找不到时不要中断部署，后续由 pip 或已安装的系统包处理。
for termux_pkg in python-cryptography python-bcrypt; do
    if pkg install -y "$termux_pkg"; then
        log "已安装 Termux 原生包：$termux_pkg"
    else
        log "跳过不可用的 Termux 包：$termux_pkg"
    fi
done

# Termux 的 python 命令名和发行版配置可能不同，优先按需求固定为 python3。
PY="python3"
command -v "$PY" >/dev/null 2>&1 || die "找不到 python3"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 版本必须为 3.10 或更高"

VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
if [[ ! -x "$VENV_PY" ]] ||
   ! grep -q '^include-system-site-packages = true$' "$VENV_DIR/pyvenv.cfg" 2>/dev/null; then
    rm -rf "$VENV_DIR"
    log "创建 venv"
    VENV_ERR="$(mktemp)"
    if ! "$PY" -m venv --system-site-packages "$VENV_DIR" 2>"$VENV_ERR"; then
        if grep -q '\[Errno 13\]' "$VENV_ERR"; then
            log "检测到权限/符号链接错误，重试 --without-pip --symlinks=False"
            rm -rf "$VENV_DIR"
            "$PY" -m venv --system-site-packages --without-pip --symlinks=False "$VENV_DIR" \
                || die "venv 创建失败：$(tr '\n' ' ' < "$VENV_ERR")"
        else
            log "标准 venv 创建失败，重试 --without-pip --symlinks=False"
            rm -rf "$VENV_DIR"
            "$PY" -m venv --system-site-packages --without-pip --symlinks=False "$VENV_DIR" \
                || die "venv 创建失败：$(tr '\n' ' ' < "$VENV_ERR")"
        fi
    fi
fi

"$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
"$VENV_PY" -m pip --version >/dev/null 2>&1 || die "venv 中无法获得 pip"
"$VENV_PY" -m pip install --upgrade pip wheel

REQ_FILE="$SCRIPT_DIR/requirements.txt"
[[ -f "$REQ_FILE" ]] || die "找不到 requirements.txt"

if command -v rustc >/dev/null 2>&1; then
    "$VENV_PY" -m pip install -r "$REQ_FILE"
else
    log "未检测到 Rust，复用 Termux 原生 cryptography/bcrypt，跳过 asyncssh 的依赖编译"
    FILTERED_REQ="$(mktemp)"
    grep -Eiv '^[[:space:]]*asyncssh([<>=!~[:space:]]|$)' "$REQ_FILE" > "$FILTERED_REQ"
    "$VENV_PY" -m pip install -r "$FILTERED_REQ"
    "$VENV_PY" -m pip install --no-deps 'asyncssh>=2.13,<3'
fi

log "Termux 部署完成：$SCRIPT_DIR"
log "启动命令：./restart.sh --no-browser"
