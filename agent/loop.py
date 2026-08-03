"""
自主 Agent 循环。
- 启动时读一次 agent/prompt.json 并缓存(记 sha256),热更新走显式的 reload_prompt()
- 接受 user_message + history,多轮工具调用,状态按 sid 从 agent.session 取
- 工具返回 ToolResult;失败且可重试的才重试 1 次
- 某次调用触发待确认时,先把这一批 tool_calls 走完(未执行的补占位 tool 消息),再停下
- history 中 ChatCompletionMessage 序列化为 dict,方便 UI 持久化
"""
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from agent import session as agent_session, tool
from agent.tool import ToolResult
from models.request import request as model_request, request_stream as model_request_stream


_PROMPT_PATH = Path(__file__).resolve().parent / "prompt.json"

_SKIPPED_TEXT = "已暂停,等待用户确认,本次调用未执行。用户确认后请用完全相同的参数重新调用。"
_COMPACT_PREFIX = "[早前读取 "
_READ_TOOLS = frozenset({"read_file"})


# ════════════════════════════════════════════════════════════
#                      提示词 & 序列化
# ════════════════════════════════════════════════════════════

_PROMPT_LOCK = threading.RLock()
_PROMPT: dict | None = None
_PROMPT_SHA = ""
_PROMPT_SOURCE = ""

_FALLBACK_PROMPT: dict = {
    "version": "fallback",
    "system": (
        "你是 CodeForge,运行在 AI Agent IDE。你拥有工具来查看、创建、修改、删除文件,运行命令,"
        "并能通过 plan 工具提交方案等用户确认。"
    ),
    "stages": {
        "plan":   "涉及创建/修改/删除文件时,先调用 plan 工具,loop 会停止等用户确认。",
        "act":    "plan 确认后,继续调文件工具或 list_dir 查目录。",
        "answer": "任务完成或无法继续时,用自然语言总结,不再调工具。",
    },
    "rules": [
        "任何 create_file / edit_file / remove_file 调用前,必须先调用 plan(plan_model 开启时)",
        "edit_file 调用前必须先 read_file 拿到当前内容,严禁凭印象修改",
        "工具失败时看错误信息调整参数,不要盲目重复同一次调用",
    ],
    "plan_schema_hint": {
        "intent":         "一句话说明任务总目标",
        "direction":      "整体修改方向(加什么/改什么/删什么)",
        "basis":          "为什么这样改,依据",
        "affected_files": ["文件绝对路径列表"],
        "steps":          ["步骤 1: ...", "步骤 2: ..."],
        "risk":           "low=只读, medium=改文件, high=删文件/大改",
    },
}


def _validate_prompt(data: Any) -> bool:
    """校验 prompt.json 的结构,任一必需字段缺失或类型不对就判为不可用。"""
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("system"), str) or not data["system"].strip():
        return False
    if not isinstance(data.get("stages"), dict) or not data["stages"]:
        return False
    if not isinstance(data.get("rules"), list) or not data["rules"]:
        return False
    if not isinstance(data.get("plan_schema_hint"), dict):
        return False
    return True


def _read_prompt_file() -> tuple[dict, str, str]:
    """读磁盘上的 prompt.json,返回 (内容, sha256, 来源)。失败时退回内置副本。"""
    try:
        raw = _PROMPT_PATH.read_bytes()
    except OSError:
        return _FALLBACK_PROMPT, "", "fallback:read_error"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _FALLBACK_PROMPT, hashlib.sha256(raw).hexdigest(), "fallback:parse_error"
    if not _validate_prompt(data):
        return _FALLBACK_PROMPT, hashlib.sha256(raw).hexdigest(), "fallback:invalid"
    return data, hashlib.sha256(raw).hexdigest(), "file"


def reload_prompt() -> dict:
    """显式重读 prompt.json 并刷新缓存,返回当前生效的提示词。"""
    global _PROMPT, _PROMPT_SHA, _PROMPT_SOURCE
    with _PROMPT_LOCK:
        _PROMPT, _PROMPT_SHA, _PROMPT_SOURCE = _read_prompt_file()
        return _PROMPT


def _load_prompt() -> dict:
    """取缓存的提示词,首次调用时读盘。运行期不会再碰磁盘。"""
    with _PROMPT_LOCK:
        if _PROMPT is None:
            return reload_prompt()
        return _PROMPT


def prompt_info() -> dict:
    """返回提示词的来源与指纹,便于 UI / 日志核对是否被改动。"""
    with _PROMPT_LOCK:
        p = _load_prompt()
        return {
            "path":    str(_PROMPT_PATH),
            "sha256":  _PROMPT_SHA,
            "source":  _PROMPT_SOURCE,
            "version": p.get("version", ""),
        }


_APPROVAL_SECTION = (
    "## 审批\n"
    "1. create_file / edit_file / remove_file / run_command 是需要确认的工具\n"
    "2. 未获授权时工具会返回 {\"status\": \"pending_approval\", ...},本轮到此为止,"
    "不要编造后续结果,也不要改用别的工具绕过\n"
    "3. 用户确认后,必须用**完全相同的参数**再调用一次同一个工具;授权只对这一次调用生效\n"
    "4. run_command 无论如何都要用户逐条确认,一次确认只放行一条命令"
)


def _build_system_prompt(plan_model: bool = True, cwd: str | None = None) -> str:
    p = _load_prompt()
    if not plan_model:
        system = (
            "你是 CodeForge,运行在 AI Agent IDE。你拥有工具来查看、创建、修改、删除文件。"
            "直接调用文件工具完成任务,不需要 plan 确认。"
        )
    else:
        system = p["system"]
    if plan_model:
        stages = "\n".join(f"- **{k}**: {v}" for k, v in p["stages"].items())
        rules  = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(p["rules"]))
        schema = json.dumps(p["plan_schema_hint"], ensure_ascii=False, indent=2)
        plan_section = (
            f"## 工作阶段\n{stages}\n\n"
            f"## 规则\n{rules}\n\n"
            f"## plan 工具参数\n```json\n{schema}\n```"
        )
    else:
        plan_section = (
            "## 工作阶段\n"
            "- **act**: 直接调用文件工具(create_file/edit_file/remove_file)完成任务,不再走 plan 工具。\n"
            "- **answer**: 任务完成时用自然语言总结。\n\n"
            "## 规则\n"
            "1. edit_file 调用前必须先 read_file 拿到当前内容,严禁凭印象修改\n"
            "2. 工具失败时按错误信息调整参数,不要盲目重复同一次调用\n"
            "3. 当前 plan_model=False,不需要 plan,但需要确认的工具仍然要走审批"
        )

    cwd_section = (
        f"\n\n## 当前工作目录\n`{cwd}`\n\n"
        f"用户在资源管理器中打开了上述目录。所有 list_dir / read_file / create_file / "
        f"edit_file / remove_file 工具调用都应当围绕此目录:\n"
        f"- 用户没指定完整路径时,把 cwd 作为前缀拼成绝对路径后再调用"
        f"(例如用户说\"在 main.py 里加一行\",工具 file_path 应当传 `{os.path.join(cwd, 'main.py')}`)\n"
        f"- 用户已指定绝对路径时,直接用用户给的(不要被 cwd 干扰)\n"
        f"- 需要列目录、读文件、写文件时,优先围绕 cwd 推断路径,无需再次询问"
    ) if cwd else ""

    return f"{system}\n\n{plan_section}\n\n{_APPROVAL_SECTION}{cwd_section}"


def _msg_to_dict(msg: Any) -> dict:
    """把 ChatCompletionMessage(可能是 BaseModel)序列化为 dict"""
    if isinstance(msg, dict):
        return msg
    if hasattr(msg, "model_dump"):
        return msg.model_dump(exclude_unset=False)
    if hasattr(msg, "to_dict"):
        return msg.to_dict()
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

def _parse_args(call: Any) -> tuple[dict | None, str]:
    """解析一次 tool_call 的参数,失败返回 (None, 错误说明)。"""
    raw = getattr(call.function, "arguments", "") or ""
    if not raw.strip():
        return {}, ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"参数 JSON 解析失败:{e}"
    if not isinstance(data, dict):
        return None, "参数必须是 JSON 对象"
    return data, ""


def _exec_with_retry(name: str, args: dict, ctx: Any, retries: int = 1) -> ToolResult:
    """执行一个工具调用,失败且标记为可重试时最多重试 retries 次。"""
    result = tool.dispatch(name, args, ctx)
    attempt = 0
    while (not result.ok) and result.retryable and attempt < retries:
        attempt += 1
        result = tool.dispatch(name, args, ctx)
    return result


# ════════════════════════════════════════════════════════════
#                        历史压缩
# ════════════════════════════════════════════════════════════

def _read_targets(messages: list) -> tuple[list, int]:
    """
    扫描历史,收集每条 read_file 结果的 (下标, 路径, 所属轮次),以及历史中的总轮次。
    轮次按「带 tool_calls 的 assistant 消息」计数。
    """
    id_path: dict[str, str] = {}
    entries: list[tuple[int, str, int]] = []
    round_idx = 0
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            round_idx += 1
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if fn.get("name") not in _READ_TOOLS:
                    continue
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                p = a.get("path") if isinstance(a, dict) else None
                if isinstance(p, str) and p:
                    id_path[tc.get("id")] = p
        elif role == "tool":
            p = id_path.get(m.get("tool_call_id"))
            if p:
                entries.append((i, p, round_idx))
    return entries, round_idx


def compact_history(messages: list, keep_rounds: int = 2) -> list:
    """
    轮次之间压缩历史,抑制 O(轮次²) 的重发膨胀。
    同一路径的旧 read_file 结果替换成占位符,只保留最后一次原文;
    最近 keep_rounds 轮的结果一律保持原样。原地修改并返回同一个列表。
    """
    entries, total_rounds = _read_targets(messages)
    if not entries:
        return messages
    last_of: dict[str, int] = {}
    for i, p, _ in entries:
        last_of[p] = i
    for i, p, r in entries:
        if i == last_of[p] or r > total_rounds - keep_rounds:
            continue
        content = messages[i].get("content") or ""
        if content.startswith(_COMPACT_PREFIX):
            continue
        messages[i] = dict(messages[i])
        messages[i]["content"] = f"{_COMPACT_PREFIX}{p},内容已被后续读取取代]"
    return messages


# ════════════════════════════════════════════════════════════
#                          主入口
# ════════════════════════════════════════════════════════════

def run(
    user_message: str,
    history: list | None = None,
    max_rounds: int = 20,
    plan_model: bool | None = None,
    cwd: str | None = None,
    sid: str | None = None,
) -> dict:
    """collect 模式:跑完一次性返回结果。run_stream 的便利封装。"""
    final = None
    for ev in run_stream(user_message, history, max_rounds, plan_model, cwd, sid=sid):
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
    use_flow: bool = False,
    sid: str | None = None,
):
    """
    流式运行 Agent,每步 yield 一个事件 dict(供 SSE 推送给前端)。
    事件:
      {"event": "start",        "max_rounds": N, "sid": "..."}
      {"event": "round",        "round": R, "max": M}
      {"event": "tool_call",    "name": "...", "args": {...}}
      {"event": "tool_result",  "name": "...", "ok": bool, "content": "...",
                                "error": str|None, "skipped": bool}
      {"event": "pending",      "pending": {...}}
      {"event": "diff_updated", "files": [...]}
      {"event": "reasoning_delta", "text": "..."}
      {"event": "content_delta",   "text": "..."}
      {"event": "done",         "answer", "history", "rounds", "stopped",
                                "tools_used", "pending", "ok", "sid"}
    """
    sess = agent_session.get(sid)
    if plan_model is None:
        with sess.lock:
            plan_model = sess.plan_model

    ctx = tool.context(sess.id)
    tools = tool.get_tools(plan_model=plan_model, sid=sess.id)
    tools_used: list[str] = []

    messages = [m for m in (history or []) if isinstance(m, dict)]
    messages = [m for m in messages if m.get("role") != "system"]
    messages.insert(0, {"role": "system", "content": _build_system_prompt(plan_model=plan_model, cwd=cwd)})
    if user_message and (not messages or messages[-1].get("role") != "user"):
        messages.append({"role": "user", "content": user_message})

    yield {"event": "start", "max_rounds": max_rounds, "sid": sess.id}

    for r in range(max_rounds):
        yield {"event": "round", "round": r + 1, "max": max_rounds}

        try:
            msg = None
            if use_flow:
                for ev in model_request_stream(messages=messages, tools=tools):
                    if ev["type"] == "reasoning":
                        yield {"event": "reasoning_delta", "text": ev["text"]}
                    elif ev["type"] == "content":
                        yield {"event": "content_delta",   "text": ev["text"]}
                    elif ev["type"] == "done":
                        msg = ev["message"]
            else:
                msg = model_request(messages=messages, tools=tools)
        except Exception as e:
            yield {
                "event":      "done",
                "answer":     f"模型调用失败:{type(e).__name__}: {e}",
                "history":    messages,
                "ok":         False,
                "stopped":    "error",
                "rounds":     r,
                "tools_used": tools_used,
                "pending":    sess.pending,
                "sid":        sess.id,
            }
            return

        # 没有 tool_calls → 收尾
        if msg is None or not getattr(msg, "tool_calls", None):
            if msg is not None:
                messages.append(_msg_to_dict(msg))
            yield {
                "event":      "done",
                "answer":     (getattr(msg, "content", "") or "") if msg is not None else "",
                "history":    messages,
                "ok":         True,
                "stopped":    "answer",
                "rounds":     r + 1,
                "tools_used": tools_used,
                "pending":    sess.pending,
                "sid":        sess.id,
            }
            return

        # 有 tool_calls → 整批走完,中途触发 pending 也要给剩余调用补齐 tool 消息
        messages.append(_msg_to_dict(msg))
        paused: dict | None = None
        for call in msg.tool_calls:
            name = call.function.name
            if name not in tools_used:
                tools_used.append(name)
            args, parse_err = _parse_args(call)
            yield {
                "event": "tool_call",
                "name":  name,
                "args":  args if args is not None else {"_raw": call.function.arguments},
            }

            skipped = paused is not None
            if skipped:
                result = ToolResult(False, _SKIPPED_TEXT, error="skipped")
            elif args is None:
                result = ToolResult(False, f"错误:{parse_err}", error="bad_arguments")
            else:
                result = _exec_with_retry(name, args, ctx, retries=1)

            yield {
                "event":   "tool_result",
                "name":    name,
                "ok":      result.ok,
                "content": result.content,
                "error":   result.error,
                "skipped": skipped,
            }
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result.content})

            if name in tool.FILE_TOOLS and not skipped and result.pending is None:
                yield {"event": "diff_updated", "files": sess.diffs.list_pending()}
            if result.pending is not None and paused is None:
                paused = result.pending

        if paused is not None:
            yield {"event": "pending", "pending": paused}
            yield {
                "event":      "done",
                "answer":     "等待用户确认",
                "history":    messages,
                "ok":         True,
                "stopped":    "pending",
                "rounds":     r + 1,
                "tools_used": tools_used,
                "pending":    sess.pending or paused,
                "sid":        sess.id,
            }
            return

        compact_history(messages)

    yield {
        "event":      "done",
        "answer":     f"未收敛(达到 {max_rounds} 轮)",
        "history":    messages,
        "ok":         False,
        "stopped":    "max_rounds",
        "rounds":     max_rounds,
        "tools_used": tools_used,
        "pending":    sess.pending,
        "sid":        sess.id,
    }
