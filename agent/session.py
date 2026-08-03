"""
会话作用域的 Agent 状态。
原先 history / plan_model / auto / pending / diff 都是进程级全局,两个标签页会互相踩。
这里按 sid 隔离,并用 RLock 保护;单用户场景默认全部落到 DEFAULT_SID,行为不变。

审批语义的关键变化:
- auto 默认 False(需要逐步确认),而不是原来的 True
- 确认不再全局打开 auto,而是写一条 approved 记录,只对「被确认的那一个动作」放行一次
"""
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SID = "default"


def _new_diff_store():
    from agent.diff import DiffStore
    return DiffStore()


@dataclass
class AgentSession:
    id: str
    history: list[dict] = field(default_factory=list)
    plan_model: bool = True
    auto: bool = False
    pending: dict | None = None
    approved: dict | None = None
    created_at: float = field(default_factory=time.time)
    touched_at: float = field(default_factory=time.time)
    diffs: Any = field(default_factory=_new_diff_store)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "sid": self.id,
                "plan_model": self.plan_model,
                "auto": self.auto,
                "pending": self.pending,
            }

    # ──────────── 审批 ────────────
    def set_pending(self, p: dict | None) -> None:
        with self.lock:
            self.pending = p

    def clear_pending(self) -> None:
        with self.lock:
            self.pending = None

    def approve_pending(self) -> dict | None:
        """
        确认当前 pending:把它转成一次性授权记录并清空 pending。
        返回被授权的 pending(没有则 None)。
        """
        with self.lock:
            p = self.pending
            if p is None:
                return None
            self.approved = {
                "action": p.get("action"),
                "args": p.get("args"),
                "nonce": uuid.uuid4().hex,
                "at": time.time(),
            }
            self.pending = None
            return p

    def consume_approval(self, action: str, args: dict) -> bool:
        """
        工具执行前调用:若存在与 (action, args) 匹配的一次性授权,消费掉并返回 True。
        比较只看关键字段(文件路径),避免模型对 content 做无关改写导致授权失效。
        """
        with self.lock:
            a = self.approved
            if not a or a.get("action") != action:
                return False
            want = (a.get("args") or {}).get("file_path")
            got = (args or {}).get("file_path")
            if want is not None and want != got:
                return False
            self.approved = None
            return True

    def clear_approval(self) -> None:
        with self.lock:
            self.approved = None


class SessionStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: dict[str, AgentSession] = {}

    def get(self, sid: str | None = None) -> AgentSession:
        key = (sid or DEFAULT_SID).strip() or DEFAULT_SID
        with self._lock:
            s = self._sessions.get(key)
            if s is None:
                s = AgentSession(id=key)
                self._sessions[key] = s
            s.touched_at = time.time()
            return s

    def drop(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def list_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions)

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()


_STORE = SessionStore()


def store() -> SessionStore:
    return _STORE


def get(sid: str | None = None) -> AgentSession:
    return _STORE.get(sid)
