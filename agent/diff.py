"""
追踪 Agent 对文件的改动,生成 unified diff 供前端展示。
- 在文件工具(create/edit/remove)执行前后抓取内容,存到 _DIFFS。
- 提供 list / get / clear 接口。
- 同名文件多次修改保留最早的 "old"、最新的 "new"。
"""
import difflib
from typing import Optional

from explorer import file as _file


_DIFFS: dict[str, dict] = {}   # path -> {"old": str|None, "new": str|None, "op": "create|edit|remove"}


def _read_safe(path: str) -> Optional[str]:
    try:
        return _file.read(path)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def snapshot_before(path: str) -> None:
    """在文件操作前抓取旧内容(不存在→None)。只记第一次,后续编辑保留原始 'old'。"""
    if path in _DIFFS:
        return
    old = _read_safe(path)
    op  = "create" if old is None else "edit"
    _DIFFS[path] = {"old": old, "new": old, "op": op}


def snapshot_after(path: str, op: str) -> None:
    """在文件操作后抓取新内容。op = create / edit / remove。"""
    if path not in _DIFFS:
        _DIFFS[path] = {"old": None, "new": None, "op": op}
    if op == "remove":
        _DIFFS[path]["new"] = None
        _DIFFS[path]["op"] = "remove"
    else:
        _DIFFS[path]["new"] = _read_safe(path) or ""
        _DIFFS[path]["op"] = op


def list_pending() -> list[dict]:
    """返回所有待展示的 diff 列表。"""
    out = []
    for path, d in _DIFFS.items():
        out.append({
            "path": path,
            "name": path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
            "op":   d["op"],
        })
    return out


def get_diff(path: str) -> Optional[dict]:
    """返回单个文件的 unified diff + 行级数据(add/del/hunk/meta/ctx)。"""
    if path not in _DIFFS:
        return None
    d   = _DIFFS[path]
    old = (d["old"] or "").splitlines()
    new = (d["new"] or "").splitlines()
    name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]

    ud = list(difflib.unified_diff(
        old, new,
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
        lineterm="",
    ))
    lines = []
    for ln in ud:
        if ln.startswith("+++") or ln.startswith("---"):
            lines.append({"type": "meta", "text": ln})
        elif ln.startswith("@@"):
            lines.append({"type": "hunk", "text": ln})
        elif ln.startswith("+"):
            lines.append({"type": "add", "text": ln})
        elif ln.startswith("-"):
            lines.append({"type": "del", "text": ln})
        else:
            lines.append({"type": "ctx", "text": ln})
    return {"path": path, "name": name, "op": d["op"], "lines": lines}


def clear(path: str | None = None) -> None:
    """清空 diff(commit 后调用)。path=None 清全部。"""
    global _DIFFS
    if path is None:
        _DIFFS = {}
    else:
        _DIFFS.pop(path, None)
