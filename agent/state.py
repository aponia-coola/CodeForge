"""
Agent 运行时状态的兼容层。
状态本体存放在 agent.session 的会话对象里(按 sid 隔离,并由会话自带的 RLock 保护),
本模块只保留原有的模块级函数,内部一律委托给对应会话,让旧调用点不改也能继续工作。
新代码请直接使用 agent.session,不要再依赖这里的进程级默认会话。
"""
from typing import Any

from agent import session as _session
from agent.session import DEFAULT_SID


# ──────────────── 会话解析 ────────────────
def get_session(sid: str | None = None) -> Any:
    """取得 sid 对应的会话对象,sid 为空落到默认会话。"""
    return _session.get(sid or DEFAULT_SID)


# ──────────────── 读 ────────────────
def get_plan_model(sid: str | None = None) -> bool:
    s = get_session(sid)
    with s.lock:
        return s.plan_model


def get_auto(sid: str | None = None) -> bool:
    s = get_session(sid)
    with s.lock:
        return s.auto


def get_pending(sid: str | None = None) -> dict | None:
    s = get_session(sid)
    with s.lock:
        return s.pending


def snapshot(sid: str | None = None) -> dict:
    """返回 {sid, plan_model, auto, pending},供 UI 初始化用。"""
    return get_session(sid).snapshot()


def has_pending(sid: str | None = None) -> bool:
    return get_pending(sid) is not None


# ──────────────── 写 ────────────────
def set_plan_model(b: bool, sid: str | None = None) -> None:
    s = get_session(sid)
    with s.lock:
        s.plan_model = bool(b)


def set_auto(b: bool, sid: str | None = None) -> None:
    s = get_session(sid)
    with s.lock:
        s.auto = bool(b)


def set_pending(p: dict | None, sid: str | None = None) -> None:
    get_session(sid).set_pending(p)


def clear_pending(sid: str | None = None) -> None:
    get_session(sid).clear_pending()


# ──────────────── 审批(一次性授权) ────────────────
def approve_pending(sid: str | None = None) -> dict | None:
    """
    确认当前 pending:转成一次性授权并清空 pending,返回被授权的 pending。
    授权只对「被确认的那一个动作」放行一次,不会打开全局 auto。
    """
    return get_session(sid).approve_pending()


def consume_approval(action: str, args: dict, sid: str | None = None) -> bool:
    """工具执行前调用:命中一次性授权则消费掉并返回 True。"""
    return get_session(sid).consume_approval(action, args)


def clear_approval(sid: str | None = None) -> None:
    get_session(sid).clear_approval()


# ──────────────── 会话管理 ────────────────
def list_sessions() -> list[str]:
    return _session.store().list_ids()


def drop_session(sid: str) -> bool:
    return _session.store().drop(sid)


def reset() -> None:
    """测试用:丢弃全部会话,回到初始状态。"""
    _session.store().reset()
