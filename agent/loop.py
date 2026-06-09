"""
自主 Agent 循环。
- 加载 agent/prompt.json 作为 system prompt
- 接受 user_message + history,多轮工具调用
- 命中 state.pending 时立即停下,返回 {stopped: "pending", pending: {...}}
- 工具失败自动重试 1 次
- history 中 ChatCompletionMessage 序列化为 dict,方便 UI 持久化
"""
import json
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


def _build_system_prompt(plan_model: bool = True) -> str:
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
    return f"{p['system']}\n\n{plan_section}"


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
    max_rounds: int = 10,
    plan_model: bool | None = None,
) -> dict:
    """
    Args:
        user_message:  本轮用户输入(可空,用于纯续接)
        history:      之前累积的消息列表
        max_rounds:   最大工具调用轮次
        plan_model:   临时覆盖 state.plan_model
    Returns:
        {
            "answer":     str,            # 本轮最终回答
            "rounds":     int,            # 实际跑了几轮
            "history":    list[dict],     # 完整 messages(供下次 run 续接)
            "ok":         bool,
            "stopped":    "answer" | "pending" | "max_rounds" | "error",
            "tools_used": list[str],
            "pending":    dict | None,    # state.pending 快照
        }
    """
    if plan_model is None:
        plan_model = state.get_plan_model()

    tools       = tool.get_tools(plan_model=plan_model)
    tools_used: list[str] = []

    messages = [m for m in (history or []) if isinstance(m, dict)]
    # 重建 system prompt 以反映当前 plan_model 状态
    messages = [m for m in messages if m.get("role") != "system"]
    messages.insert(0, {"role": "system", "content": _build_system_prompt(plan_model=plan_model)})
    if user_message and (not messages or messages[-1].get("role") != "user"):
        messages.append({"role": "user", "content": user_message})

    for r in range(max_rounds):
        try:
            msg = model_request(messages=messages, tools=tools)
        except Exception as e:
            return {
                "answer":     f"模型调用失败:{type(e).__name__}: {e}",
                "rounds":     r,
                "history":    messages,
                "ok":         False,
                "stopped":    "error",
                "tools_used": tools_used,
                "pending":    state.get_pending(),
            }

        # 没有 tool_calls → 收尾
        if not msg.tool_calls:
            messages.append(_msg_to_dict(msg))
            return {
                "answer":     msg.content or "",
                "rounds":     r + 1,
                "history":    messages,
                "ok":         True,
                "stopped":    "answer",
                "tools_used": tools_used,
                "pending":    state.get_pending(),
            }

        # 有 tool_calls → 记录并逐个执行
        messages.append(_msg_to_dict(msg))
        for call in msg.tool_calls:
            if call.function.name not in tools_used:
                tools_used.append(call.function.name)
            result = _exec_with_retry(call, retries=1)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            # 任一工具触发 pending → 整轮停下
            if state.get_pending() is not None:
                return {
                    "answer":     "等待用户确认",
                    "rounds":     r + 1,
                    "history":    messages,
                    "ok":         True,
                    "stopped":    "pending",
                    "tools_used": tools_used,
                    "pending":    state.get_pending(),
                }

    return {
        "answer":     f"未收敛(达到 {max_rounds} 轮)",
        "rounds":     max_rounds,
        "history":    messages,
        "ok":         False,
        "stopped":    "max_rounds",
        "tools_used": tools_used,
        "pending":    state.get_pending(),
    }
