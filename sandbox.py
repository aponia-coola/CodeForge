"""
路径沙箱。
- 所有文件读写都必须先过 resolve():把路径规范化,并校验是否落在允许的根目录内
- 保护 CodeForge 自身的治理文件(prompt.json / model.json / .config.json)不被 agent 改写
- 根目录可在 .config.json 的 workspace_roots 配置;缺省为用户主目录(移动端追加 /sdcard)
"""
import json
import os
import sys
from pathlib import Path

_INSTALL_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _INSTALL_DIR / ".config.json"

# agent 工具永远不能写的文件:改了它们等于改 agent 自己的行为
_PROTECTED_WRITE = {
    (_INSTALL_DIR / "agent" / "prompt.json").resolve(),
    (_INSTALL_DIR / "models" / "model.json").resolve(),
    _CONFIG_PATH.resolve(),
    (_INSTALL_DIR / "sandbox.py").resolve(),
}


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _default_roots() -> list[Path]:
    roots = [Path(os.path.expanduser("~")).resolve()]
    sdcard = Path("/sdcard")
    try:
        if sdcard.is_dir():
            roots.append(sdcard.resolve())
    except OSError:
        pass
    if _INSTALL_DIR not in roots and not any(_is_under(_INSTALL_DIR, r) for r in roots):
        roots.append(_INSTALL_DIR)
    return roots


def _read_configured_roots() -> list[Path] | None:
    try:
        cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    raw = cfg.get("workspace_roots")
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            out.append(Path(os.path.expanduser(item.strip())).resolve())
        except OSError:
            continue
    return out or None


_ROOTS: list[Path] = _read_configured_roots() or _default_roots()


def get_roots() -> list[str]:
    return [str(r) for r in _ROOTS]


def set_roots(paths: list[str]) -> None:
    """测试/启动参数用:替换允许的根目录集合。"""
    global _ROOTS
    resolved = []
    for p in paths:
        resolved.append(Path(os.path.expanduser(p)).resolve())
    if resolved:
        _ROOTS = resolved


class SandboxError(PermissionError):
    """路径越界或写入受保护文件。"""


def _normalize(path: str) -> Path:
    if not path or not str(path).strip():
        raise SandboxError("路径为空")
    p = Path(os.path.expanduser(str(path).strip()))
    if not p.is_absolute():
        p = (Path.cwd() / p)
    # resolve() 会跟随符号链接,防止用软链跳出根目录
    try:
        return p.resolve()
    except OSError as e:
        raise SandboxError(f"无法解析路径: {path} ({e})")


def is_protected(path: str) -> bool:
    try:
        return _normalize(path) in _PROTECTED_WRITE
    except SandboxError:
        return False


def resolve(path: str, *, write: bool = False, agent: bool = False) -> str:
    """
    规范化并校验路径。
    write=True  额外校验父目录也在根内(防止通过不存在的路径写到外面)
    agent=True  额外拒绝写入受保护的治理文件
    越界抛 SandboxError。
    """
    p = _normalize(path)
    if not any(_is_under(p, r) or p == r for r in _ROOTS):
        raise SandboxError(
            f"路径超出允许范围: {p}\n允许的根目录: {', '.join(str(r) for r in _ROOTS)}"
        )
    if write:
        parent = p.parent
        if not any(_is_under(parent, r) or parent == r for r in _ROOTS):
            raise SandboxError(f"父目录超出允许范围: {parent}")
        if agent and p in _PROTECTED_WRITE:
            raise SandboxError(
                f"受保护文件,agent 不得改写: {p}\n"
                f"这是 CodeForge 自身的配置/提示词文件,如需修改请在编辑器中手动操作。"
            )
    return str(p)


def safe_join(parent: str, name: str) -> str:
    """把 name 拼到 parent 下,拒绝任何路径分隔符和 .. """
    if not name or name.strip() != name:
        raise SandboxError("名称为空或首尾有空白")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise SandboxError("名称不能包含路径分隔符")
    if sys.platform == "win32" and ":" in name:
        raise SandboxError("名称不能包含冒号")
    base = _normalize(parent)
    return resolve(str(base / name), write=True)
