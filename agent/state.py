"""
Agent 运行时状态管理。
UI / 测试 / 后台任务通过这个模块读写状态,避免文件 I/O。
"""
from typing import Any

# ──────────────── 状态存储 ────────────────
_STATE: dict[str, Any] = {
    "plan_model": True,    # True=注册 plan 工具,模型必须先 plan 再 act;False=隐藏 plan 工具
    "auto":      True,     # True=文件工具立即执行;False=返回方案待用户确认
    "pending":   None,     # {"action": "...", "args": {...}, "markdown": "..."}
}


# ──────────────── 读 ────────────────
def get_plan_model() -> bool:                  return _STATE["plan_model"]
def get_auto()      -> bool:                   return _STATE["auto"]
def get_pending()   -> dict | None:            return _STATE["pending"]
def snapshot()      -> dict:                   return {
    "plan_model": _STATE["plan_model"],
    "auto":      _STATE["auto"],
    "pending":   _STATE["pending"],
}


# ──────────────── 写(给 UI / 测试 / 后台任务用) ────────────────
def set_plan_model(b: bool)        -> None:    _STATE["plan_model"] = bool(b)
def set_auto(b: bool)              -> None:    _STATE["auto"] = bool(b)
def set_pending(p: dict | None)    -> None:    _STATE["pending"] = p
def clear_pending()                -> None:    _STATE["pending"] = None


# ──────────────── 给 UI 接入用的快捷判断 ────────────────
def has_pending() -> bool:                     return _STATE["pending"] is not None
