"""
自主 Agent 循环。
- 加载 agent/prompt.json 作为 system prompt
- 接受 user_message + history,多轮工具调用
- 命中 state.pending 时立即停下,返回 {stopped: "pending", pending: {...}}
- 工具失败自动重试 1 次
- history 中 ChatCompletionMessage 序列化为 dict,方便 UI 持久化
"""
import json
import os
from pathlib import Path
from typing import Any

from agent import state, tool
from models.request import request as model_request


_PROMPT_PATH = Path(__file__).resolve().parent / "prompt.json"


# ════════════════════════════════════════════════════════════
#                      提示词 & 序列化
# ════════════════════════════════════════════════════════════

def _load_prompt() -> dict:
    return json.loads(_PROMPT_PATH.read_text(encoding="utf-8"))


def _build_system_prompt(plan_model: bool = True, cwd: str | None = None) -> str:
    p = _load_prompt()
    if plan_model:
        stages = "\n".join(f"- **{k}**: {v}" for k, v in p["stages"].items())
        rules  = "\n".join(f"{i+1}. {r}" for i, r in enumerate(p["rules"]))
        schema = json.dumps(p["plan_schema_hint"], ensure_ascii=False, indent=2)
        plan_section = (
            f"## 工作阶段\n{stages}\n\n"
            f"## 规则\n{rules}\n\n"
            f"## plan 工具参数\n```json\n{schema}\n```"
        )
    else:
        # plan_model=False: 简化模式,直接 act
        plan_section = (
            "## 工作阶段\n"
            "- **act**: 直接调用文件工具(create_file/edit_file/remove_file)完成任务,不再走 plan 工具。\n"
            "- **answer**: 任务完成时用自然语言总结。\n\n"
            "## 规则\n"
            "1. edit_file 调用前必须先 read 或 list_dir 拿到当前内容,严禁凭印象修改\n"
            "2. 工具失败可重试 1 次(loop 自动处理),仍失败就放弃并告知用户\n"
            "3. 当前 plan_model=False,所有文件工具 auto=True 时立即执行,无需 plan 确认"
        )

    # 当前工作目录(从资源管理器同步):仅当 cwd 非空时注入,告诉模型把"用户给的相对路径"拼成绝对路径
    cwd_section = (
        f"\n\n## 当前工作目录\n`{cwd}`\n\n"
        f"用户在资源管理器中打开了上述目录。所有 list_dir / read_file / create_file / "
        f"edit_file / remove_file 工具调用都应当围绕此目录:\n"
        f"- 用户没指定完整路径时,把 cwd 作为前缀拼成绝对路径后再调用"
        f"(例如用户说\"在 main.py 里加一行\",工具 file_path 应当传 `{os.path.join(cwd, 'main.py')}`)\n"
        f"- 用户已指定绝对路径时,直接用用户给的(不要被 cwd 干扰)\n"
        f"- 需要列目录、读文件、写文件时,优先围绕 cwd 推断路径,无需再次询问"
    ) if cwd else ""

    return f"{p['system']}\n\n{plan_section}{cwd_section}"


def _msg_to_dict(msg: Any) -> dict:
    """把 ChatCompletionMessage(可能是 BaseModel)序列化为 dict"""
    if isinstance(msg, dict):
        return msg
    if hasattr(msg, "model_dump"):
        return msg.model_dump(exclude_unset=False)
    if hasattr(msg, "to_dict"):
        return msg.to_dict()
    # fallback: 走 __dict__
    d = {"role": getattr(msg, "role", "assistant"), "content": getattr(msg, "content", "") or ""}
    if getattr(msg, "tool_calls", None):
        d["tool_calls"] = [
            {
                "id":       tc.id,
                "type":     "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    if getattr(msg, "refusal", None):
        d["refusal"] = msg.refusal
    return d


# ════════════════════════════════════════════════════════════
#                      工具执行 + 重试
# ════════════════════════════════════════════════════════════

def _exec_with_retry(call: Any, retries: int = 1) -> str:
    """执行一个工具调用,失败重试 retries 次。返回字符串结果"""
    name = call.function.name
    try:
        args = json.loads(call.function.arguments) if call.function.arguments else {}
    except json.JSONDecodeError as e:
        return f"参数解析失败:{e}"
    last_err = None
    for attempt in range(retries + 1):
        try:
            return tool.call(name, **args)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt >= retries:
                break
    return f"工具 {name} 失败(重试 {retries} 次后放弃):{last_err}"


# ════════════════════════════════════════════════════════════
#                          主入口
# ════════════════════════════════════════════════════════════

def run(
    user_message: str,
    history: list | None = None,
    max_rounds: int = 20,
    plan_model: bool | None = None,
    cwd: str | None = None,
) -> dict:
    """collect 模式:跑完一次性返回结果。run_stream 的便利封装。"""
    final = None
    for ev in run_stream(user_message, history, max_rounds, plan_model, cwd):
        if ev.get("event") == "done":
            final = ev
    return final or {"answer": "", "history": [], "ok": False, "stopped": "error",
                     "rounds": 0, "tools_used": [], "pending": None}


def run_stream(
    user_message: str,
    history: list | None = None,
    max_rounds: int = 20,
    plan_model: bool | None = None,
    cwd: str | None = None,
):
    """
    流式运行 Agent,每步 yield 一个事件 dict(供 SSE 推送给前端)。
    事件:
      {"event": "start",        "max_rounds": N}
      {"event": "round",        "round": R, "max": M}
      {"event": "tool_call",    "name": "...", "args": {...}}
      {"event": "tool_result",  "name": "...", "ok": bool, "content": "..."}
      {"event": "pending",      "pending": {...}}
      {"event": "done",         "answer", "history", "rounds", "stopped",
                                "tools_used", "pending", "ok"}
    """
    if plan_model is None:
        plan_model = state.get_plan_model()

    # ── "确认/继续/OK" → 清 pending + 临时开 auto(详见 run 的注释) ──
    _CONFIRM_WORDS = {"确认", "继续", "ok", "OK", "Ok", "yes", "Yes", "YES",
                      "确认吧", "可以", "批准", "approve", "Approved", "APPROVED"}
    _saved_auto: bool | None = None
    if user_message and user_message.strip() in _CONFIRM_WORDS:
        state.clear_pending()
        _saved_auto = state.get_auto()
        state.set_auto(True)

    tools       = tool.get_tools(plan_model=plan_model)
    tools_used: list[str] = []

    messages = [m for m in (history or []) if isinstance(m, dict)]
    messages = [m for m in messages if m.get("role") != "system"]
    messages.insert(0, {"role": "system", "content": _build_system_prompt(plan_model=plan_model, cwd=cwd)})
    if user_message and (not messages or messages[-1].get("role") != "user"):
        messages.append({"role": "user", "content": user_message})

    try:
        yield {"event": "start", "max_rounds": max_rounds}

        for r in range(max_rounds):
            yield {"event": "round", "round": r + 1, "max": max_rounds}

            try:
                msg = model_request(messages=messages, tools=tools)
            except Exception as e:
                yield {
                    "event":     "done",
                    "answer":    f"模型调用失败:{type(e).__name__}: {e}",
                    "history":   messages,
                    "ok":        False,
                    "stopped":   "error",
                    "rounds":    r,
                    "tools_used": tools_used,
                    "pending":   state.get_pending(),
                }
                return

            # 没有 tool_calls → 收尾
            if not msg.tool_calls:
                messages.append(_msg_to_dict(msg))
                yield {
                    "event":      "done",
                    "answer":     msg.content or "",
                    "history":    messages,
                    "ok":         True,
                    "stopped":    "answer",
                    "rounds":     r + 1,
                    "tools_used": tools_used,
                    "pending":    state.get_pending(),
                }
                return

            # 有 tool_calls → 记录 + 逐个执行,边执行边 yield
            messages.append(_msg_to_dict(msg))
            for call in msg.tool_calls:
                if call.function.name not in tools_used:
                    tools_used.append(call.function.name)
                try:
                    args = json.loads(call.function.arguments) if call.function.arguments else {}
                except json.JSONDecodeError:
                    args = {"_raw": call.function.arguments}
                yield {
                    "event": "tool_call",
                    "name":  call.function.name,
                    "args":  args,
                }
                result = _exec_with_retry(call, retries=1)
                ok = not (isinstance(result, str) and result.startswith("工具 ") and "执行失败" in result)
                yield {
                    "event":   "tool_result",
                    "name":    call.function.name,
                    "ok":      ok,
                    "content": result,
                }
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
                # 任一工具触发 pending → 整轮停下
                if state.get_pending() is not None:
                    pending = state.get_pending()
                    yield {"event": "pending", "pending": pending}
                    yield {
                        "event":      "done",
                        "answer":     "等待用户确认",
                        "history":    messages,
                        "ok":         True,
                        "stopped":    "pending",
                        "rounds":     r + 1,
                        "tools_used": tools_used,
                        "pending":    pending,
                    }
                    return

        yield {
            "event":      "done",
            "answer":     f"未收敛(达到 {max_rounds} 轮)",
            "history":    messages,
            "ok":         False,
            "stopped":    "max_rounds",
            "rounds":     max_rounds,
            "tools_used": tools_used,
            "pending":    state.get_pending(),
        }
    finally:
        if _saved_auto is not None:
            state.set_auto(_saved_auto)
