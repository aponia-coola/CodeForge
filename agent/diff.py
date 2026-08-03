"""
追踪 Agent 对文件的改动,生成 unified diff 供前端展示。
- 一个文件一条 FileChange 记录:基线、提议内容、是否已落盘全在同一条里,不存在两套存储互相脱节
- 每个会话一个 DiffStore 实例;模块级同名函数委托给一个默认实例,旧调用点无需改动
- 所有路径先过 sandbox.resolve(write=True, agent=True),写盘走 explorer.file 的原子写
- 同一文件多次改动只保留最早的基线,保证撤销一定回到 agent 动手之前的状态
"""
import difflib
import hashlib
import os
import sys
import threading
from dataclasses import dataclass
from typing import Optional

try:
    import sandbox
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import sandbox

from explorer import file as _file


# ════════════════════════════════════════════════════════════
#                        记录与工具函数
# ════════════════════════════════════════════════════════════
@dataclass
class FileChange:
    """
    一个文件的一条改动记录。

    path            沙箱校验后的绝对路径,同时用作记录的键
    baseline        agent 动手之前的原文,None 表示当时文件不存在
    proposed        改动之后的内容,None 表示文件已被删除
    op              create / edit / remove,始终由 baseline 与是否删除推导
    staged          True 表示 proposed 还只在内存里等用户确认,False 表示已经落盘
    baseline_mtime  生成 patch 时磁盘的 mtime,仅作诊断(FAT/exFAT 上粒度 2 秒,不可信)
    baseline_sha    生成 patch 时磁盘内容的 sha256,apply 前用它判断文件有没有被外部改过
    encoding        原文件编码,写回时沿用
    newline         原文件主导行尾,写回时沿用
    """
    path: str
    baseline: Optional[str] = None
    proposed: Optional[str] = None
    op: str = "edit"
    staged: bool = False
    baseline_mtime: Optional[float] = None
    baseline_sha: Optional[str] = None
    encoding: str = _file.DEFAULT_ENCODING
    newline: str = "\n"


def _op_for(baseline: Optional[str], removed: bool = False) -> str:
    """由基线和删除标记推导 op,避免 op 与内容各说各话。"""
    if removed:
        return "remove"
    return "create" if baseline is None else "edit"


def _to_lf(text: str) -> str:
    """把任意行尾统一成 LF,diff / patch / 哈希都在 LF 空间里做。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _sha256(text: str) -> str:
    """文本内容的 sha256,用于比对磁盘是否被外部改动。"""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _basename(path: str) -> str:
    """取路径最后一段,同时兼容两种分隔符。"""
    return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def _mtime(path: str) -> Optional[float]:
    """取 mtime,取不到返回 None。"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# ════════════════════════════════════════════════════════════
#                          DiffStore
# ════════════════════════════════════════════════════════════
class DiffStore:
    """
    一个会话的改动池。
    公开方法自带锁,可以被 Flask 的多个请求线程同时调用。
    """

    def __init__(self):
        self._changes: dict[str, FileChange] = {}
        self._lock = threading.RLock()

    # ──────────── 路径与磁盘 ────────────
    def _key(self, path: str) -> str:
        """把外部传入的路径归一成沙箱校验过的绝对路径,越界或受保护抛 SandboxError。"""
        return sandbox.resolve(path, write=True, agent=True)

    def _lookup_key(self, path: str) -> Optional[str]:
        """只读查询用的宽松版本,路径非法时返回 None,等价于查不到记录。"""
        try:
            return self._key(path)
        except (sandbox.SandboxError, OSError, ValueError):
            return None

    def _read_disk(self, full: str) -> tuple[str, str, str]:
        """
        读磁盘原文,返回 (LF 归一化文本, 编码, 主导行尾)。
        读的是不带行号的原始内容,文件不存在 / 二进制 / 超出大小上限都会抛异常。
        """
        text, encoding = _file.read_text_with_encoding(full)
        return _to_lf(text), encoding, _file.detect_newline(text)

    # ──────────── 快照 ────────────
    def snapshot_before(self, path: str) -> None:
        """
        在文件操作前抓取旧内容(文件不存在→None)。
        已经有记录就直接返回,保证多次改动保留最早的基线。
        读不出内容的文件(二进制/超限)不记录,避免被误判成新建后撤销时误删。
        """
        full = self._lookup_key(path)
        if full is None:
            return
        with self._lock:
            if full in self._changes:
                return
            if os.path.exists(full):
                try:
                    baseline, encoding, newline = self._read_disk(full)
                except Exception:
                    return
            else:
                baseline, encoding, newline = None, _file.DEFAULT_ENCODING, "\n"
            self._changes[full] = FileChange(
                path=full,
                baseline=baseline,
                proposed=baseline,
                op=_op_for(baseline),
                staged=False,
                encoding=encoding,
                newline=newline,
            )

    def snapshot_after(self, path: str, op: str) -> None:
        """
        在文件操作后抓取新内容,记录转为已落盘状态。op = create / edit / remove。
        磁盘已经是最终内容,所以 staged 置 False,并清掉 patch 期的冲突校验信息。
        """
        full = self._lookup_key(path)
        if full is None:
            return
        with self._lock:
            rec = self._changes.get(full)
            if rec is None:
                rec = FileChange(path=full, baseline=None, proposed=None, op=op)
                self._changes[full] = rec
            if op == "remove":
                rec.proposed = None
                rec.op = _op_for(rec.baseline, removed=True)
            else:
                try:
                    text, encoding, newline = self._read_disk(full)
                except Exception:
                    text, encoding, newline = rec.baseline, rec.encoding, rec.newline
                rec.proposed = text
                rec.encoding = encoding
                rec.newline = newline
                rec.op = _op_for(rec.baseline)
            rec.staged = False
            rec.baseline_sha = None
            rec.baseline_mtime = None

    # ──────────── 查询 ────────────
    def list_pending(self) -> list[dict]:
        """
        返回所有待展示的改动。
        remove 也在列表里:agent 删掉的文件必须在审查界面留下卡片和恢复入口。
        """
        with self._lock:
            return [
                {
                    "path":   rec.path,
                    "name":   _basename(rec.path),
                    "op":     rec.op,
                    "staged": rec.staged,
                }
                for rec in self._changes.values()
            ]

    def get_diff(self, path: str) -> Optional[dict]:
        """返回单个文件的 unified diff + 行级数据(add/del/hunk/meta/ctx)。"""
        full = self._lookup_key(path)
        with self._lock:
            rec = self._changes.get(full) if full else None
            if rec is None:
                return None
            old = (rec.baseline or "").splitlines()
            new = (rec.proposed or "").splitlines()
            op = rec.op
        name = _basename(full)

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
        return {"path": full, "name": name, "op": op, "lines": lines}

    def get_baseline(self, path: str) -> Optional[str]:
        """返回 agent 改动前的原文(编辑器红绿高亮的基线),没有记录返回 None。"""
        full = self._lookup_key(path)
        with self._lock:
            rec = self._changes.get(full) if full else None
            return rec.baseline if rec else None

    def get_preview(self, path: str) -> Optional[str]:
        """返回还没落盘的预览全文,没有待确认内容时返回 None。"""
        full = self._lookup_key(path)
        with self._lock:
            rec = self._changes.get(full) if full else None
            if rec is None or not rec.staged:
                return None
            return rec.proposed

    def has_pending(self, path: str) -> bool:
        """该文件是否有还没落盘、等待用户确认的内容。"""
        full = self._lookup_key(path)
        with self._lock:
            rec = self._changes.get(full) if full else None
            return bool(rec and rec.staged)

    # ──────────── Pending Patch ────────────
    def store_patch(self, path: str, patches: list[dict]) -> dict:
        """
        生成 patch 预览(不写磁盘)。patches: [{"old": "...", "new": "..."}, ...]
        同时记录当前磁盘内容的 sha256 与 mtime,apply 时用来发现文件被外部改动。
        已有记录时沿用它的基线,不会把之前的 create 快照冲掉。
        """
        try:
            full = self._key(path)
        except (sandbox.SandboxError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        try:
            current, encoding, newline = self._read_disk(full)
        except Exception as e:
            return {"ok": False, "error": f"读取文件失败: {e}"}
        try:
            proposed = _file.apply_patches_content(current, patches)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if proposed == current:
            return {"ok": False, "error": "patch 未产生任何变化"}

        with self._lock:
            rec = self._changes.get(full)
            baseline = rec.baseline if rec is not None else current
            self._changes[full] = FileChange(
                path=full,
                baseline=baseline,
                proposed=proposed,
                op=_op_for(baseline),
                staged=True,
                baseline_mtime=_mtime(full),
                baseline_sha=_sha256(current),
                encoding=encoding,
                newline=newline,
            )
        return {"ok": True}

    def apply_patch(self, path: str) -> dict:
        """
        用户确认保留 → 把预览内容原子写入磁盘。
        只有 staged 记录才需要写盘;create/remove/直接编辑的内容磁盘上已经是对的,
        再写一次只会用展示用的内容覆坏源文件,所以直接收下记录不动磁盘。
        写盘前重新读盘比对 sha256,与生成 patch 时不一致就拒绝,让用户重新生成。
        """
        full = self._lookup_key(path)
        with self._lock:
            rec = self._changes.get(full) if full else None
            if rec is None:
                return {"ok": False, "error": "该文件没有待确认的 patch(后端可能已重启,请重新生成)"}
            if not rec.staged or rec.proposed is None:
                self._changes.pop(full, None)
                return {"ok": True, "op": rec.op, "applied": False}
            try:
                current, _, _ = self._read_disk(full)
            except FileNotFoundError:
                return {"ok": False, "error": "文件已不存在,无法应用 patch,请重新生成"}
            except Exception as e:
                return {"ok": False, "error": f"读取文件失败: {e}"}
            if rec.baseline_sha and _sha256(current) != rec.baseline_sha:
                return {
                    "ok": False,
                    "conflict": True,
                    "error": "文件已被外部修改(内容与生成 patch 时不一致),请重新生成 patch",
                }
            try:
                _file.write_text(
                    full, rec.proposed,
                    encoding=rec.encoding, newline=rec.newline, agent=True,
                )
            except Exception as e:
                return {"ok": False, "error": str(e)}
            self._changes.pop(full, None)
            return {"ok": True, "op": rec.op, "applied": True}

    def discard_patch(self, path: str) -> dict:
        """用户撤销 → 丢弃整条记录(不动磁盘)。"""
        full = self._lookup_key(path)
        with self._lock:
            if full:
                self._changes.pop(full, None)
        return {"ok": True}

    # ──────────── 撤销与清理 ────────────
    def revert(self, path: str) -> dict:
        """
        把文件恢复到 agent 动手之前:
        - baseline is None(agent 新建的文件)→ 删除该文件
        - 其余情况(编辑 / 删除)→ 把 baseline 原子写回
        成功后整条记录移除,pending 状态一并消失。
        """
        full = self._lookup_key(path)
        with self._lock:
            rec = self._changes.get(full) if full else None
            if rec is None:
                return {"ok": False, "error": "该文件没有可撤销的改动"}
            op = rec.op
            try:
                if rec.baseline is None:
                    self._delete(full)
                else:
                    self._restore(rec)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            self._changes.pop(full, None)
            return {"ok": True, "op": op}

    def _delete(self, full: str) -> None:
        """删除 agent 新建的文件,已经不在就什么都不做。"""
        if os.path.exists(full):
            _file.remove_file(full, agent=True)

    def _restore(self, rec: FileChange) -> None:
        """把基线原子写回,内容已经一致就不动磁盘,父目录缺失时先建回来。"""
        try:
            current, _, _ = self._read_disk(rec.path)
        except Exception:
            current = None
        if current == rec.baseline:
            return
        parent = os.path.dirname(rec.path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        _file.write_text(
            rec.path, rec.baseline,
            encoding=rec.encoding, newline=rec.newline, agent=True,
        )

    def clear(self, path: Optional[str] = None) -> None:
        """清空记录(不动磁盘)。path=None 清全部。"""
        with self._lock:
            if path is None:
                self._changes.clear()
                return
            full = self._lookup_key(path)
            if full:
                self._changes.pop(full, None)


# ════════════════════════════════════════════════════════════
#                     默认实例与模块级委托
# ════════════════════════════════════════════════════════════
# main.py / agent.tool / agent.loop 仍按模块级函数调用,
# 这里统一委托给同一个默认实例,行为与按会话取实例完全一致。
_DEFAULT = DiffStore()


def default_store() -> DiffStore:
    """返回模块级默认实例。"""
    return _DEFAULT


def snapshot_before(path: str) -> None:
    """委托:在文件操作前抓取旧内容。"""
    _DEFAULT.snapshot_before(path)


def snapshot_after(path: str, op: str) -> None:
    """委托:在文件操作后抓取新内容。"""
    _DEFAULT.snapshot_after(path, op)


def list_pending() -> list[dict]:
    """委托:返回所有待展示的改动(含 remove)。"""
    return _DEFAULT.list_pending()


def get_diff(path: str) -> Optional[dict]:
    """委托:返回单个文件的 unified diff。"""
    return _DEFAULT.get_diff(path)


def get_baseline(path: str) -> Optional[str]:
    """委托:返回 agent 改动前的原文。"""
    return _DEFAULT.get_baseline(path)


def get_preview(path: str) -> Optional[str]:
    """委托:返回还没落盘的预览全文。"""
    return _DEFAULT.get_preview(path)


def revert(path: str) -> dict:
    """委托:把文件恢复到 agent 改动前。"""
    return _DEFAULT.revert(path)


def clear(path: Optional[str] = None) -> None:
    """委托:清空记录,path=None 清全部。"""
    _DEFAULT.clear(path)


def store_patch(path: str, patches: list[dict]) -> dict:
    """委托:生成 patch 预览。"""
    return _DEFAULT.store_patch(path, patches)


def apply_patch(path: str) -> dict:
    """委托:把预览内容写入磁盘。"""
    return _DEFAULT.apply_patch(path)


def discard_patch(path: str) -> dict:
    """委托:丢弃预览。"""
    return _DEFAULT.discard_patch(path)


def has_pending(path: str) -> bool:
    """委托:该文件是否有待确认的内容。"""
    return _DEFAULT.has_pending(path)
