"""
工具注册表。
- @tool 装饰器:把函数注册成 OpenAI 兼容的工具(JSON Schema)
- get_tools(plan_model=True) -> list[dict]:  返回给模型的 tools 列表
- call(name, **kwargs) -> str:               执行一个已注册的工具
- 状态联动:auto=False 时,文件类工具不执行,写 state.pending
"""
import json
import os
from typing import Callable

from agent import state
from agent.explor import file


# ──────────────── 注册表 ────────────────
_REGISTRY: dict[str, dict] = {}   # name -> {"func": Callable, "schema": dict}


def tool(name: str, description: str, parameters: dict):
    """装饰器:把函数注册为 OpenAI 兼容工具(JSON Schema)"""
    def decorator(func: Callable) -> Callable:
        _REGISTRY[name] = {
            "func":   func,
            "schema": {
                "type": "function",
                "function": {
                    "name":        name,
                    "description": description,
                    "parameters":  parameters,
                },
            },
        }
        return func
    return decorator


# ──────────────── 工具调用入口 ────────────────
def call(name: str, **kwargs) -> str:
    """执行一个已注册的工具,异常转字符串返回(不会抛出)"""
    if name not in _REGISTRY:
        return f"错误:未知工具 {name}"
    try:
        return str(_REGISTRY[name]["func"](**kwargs))
    except Exception as e:
        return f"工具 {name} 执行失败:{type(e).__name__}: {e}"


# ──────────────── 获取当前可用工具列表 ────────────────
def get_tools(plan_model: bool | None = None) -> list[dict]:
    """
    返回 OpenAI 格式的 tools 列表。
    plan_model=False 时不返回 plan 工具(模型不知道要 plan)。
    """
    if plan_model is None:
        plan_model = state.get_plan_model()

    out = []
    for name, entry in _REGISTRY.items():
        if name == "plan" and not plan_model:
            continue
        out.append(entry["schema"])
    return out


# ════════════════════════════════════════════════════════════
#                        工具实现
# ════════════════════════════════════════════════════════════

@tool(
    name="plan",
    description=(
        "在执行任何 create_file / edit_file / remove_file 之前,必须先调用此工具提交结构化方案。"
        "调用后 loop 会停止,等待用户在 chat 中确认。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "intent":         {"type": "string",  "description": "一句话说明任务总目标"},
            "direction":      {"type": "string",  "description": "整体修改方向(加什么/改什么/删什么)"},
            "basis":          {"type": "string",  "description": "为什么这样改,依据(用户原话/上下文/常识)"},
            "affected_files": {"type": "array", "items": {"type": "string"}, "description": "要操作的文件路径"},
            "steps":          {"type": "array", "items": {"type": "string"}, "description": "步骤列表"},
            "risk":           {"type": "string",  "enum": ["low", "medium", "high"], "description": "low=只读,medium=改文件,high=删文件/大改"},
        },
        "required": ["intent", "direction", "basis", "affected_files", "steps", "risk"],
    },
)
def _plan_tool(intent: str, direction: str, basis: str, affected_files: list, steps: list, risk: str) -> str:
    plan = {
        "intent": intent, "direction": direction, "basis": basis,
        "affected_files": affected_files, "steps": steps, "risk": risk,
    }
    md = (
        f"## 📋 方案确认\n\n"
        f"**目标**: {intent}\n\n"
        f"**方向**: {direction}\n\n"
        f"**依据**: {basis}\n\n"
        f"**涉及文件**:\n" + "\n".join(f"- `{f}`" for f in affected_files) + "\n\n"
        f"**步骤**:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps)) + "\n\n"
        f"**风险**: {risk}\n"
    )
    state.set_pending({
        "action":   "plan",
        "args":     plan,
        "markdown": md,
        "message":  "用户在 chat 中确认后,agent 才会继续。",
    })
    return md


@tool(
    name="list_dir",
    description="列出指定目录下的文件夹和文件(默认不显示隐藏文件)",
    parameters={
        "type": "object",
        "properties": {
            "path":        {"type": "string",  "description": "目录绝对路径"},
            "show_hidden": {"type": "boolean", "default": False, "description": "是否显示 . 开头的隐藏文件"},
        },
        "required": ["path"],
    },
)
def _list_dir_tool(path: str, show_hidden: bool = False) -> str:
    result = file.list_dir(path, show_hidden=show_hidden)
    return json.dumps(result, ensure_ascii=False, indent=2)


@tool(
    name="read_file",
    description="按行号读取文件内容(行号从 1 开始)。start_line 缺省则从第 1 行读到末尾。",
    parameters={
        "type": "object",
        "properties": {
            "path":       {"type": "string",  "description": "文件绝对路径"},
            "start_line": {"type": "integer", "description": "起始行号(1-indexed,包含)"},
        },
        "required": ["path"],
    },
)
def _read_file_tool(path: str, start_line: int | None = None) -> str:
    return file.read(path, start_line=start_line)


@tool(
    name="create_file",
    description=(
        "创建新文件(**已存在会报错,禁止覆盖,避免误删内容**)。如需修改已有文件,用 edit_file。"
        "可在创建时直接写入 content。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string",  "description": "新文件的绝对路径"},
            "content":   {"type": "string",  "default": "", "description": "初始内容(可选)"},
            "auto":      {"type": "boolean", "default": True,
                          "description": "True=立即执行;False=返回方案待确认。state.auto=False 时此参数无效,始终暂停。"},
        },
        "required": ["file_path"],
    },
)
def _create_file(file_path: str, content: str = "", auto: bool = True) -> str:
    if not state.get_auto() or not auto:
        pending = {"action": "create_file", "args": {"file_path": file_path, "content": content}}
        state.set_pending(pending)
        return json.dumps({"status": "pending_approval", **pending}, ensure_ascii=False)
    if os.path.exists(file_path):
        raise FileExistsError(f"文件已存在,禁止覆盖: {file_path}。如需修改请用 edit_file")
    file.create(file_path)
    if content:
        file.change(file_path, content, mode='append')
    return f"已创建 {file_path}({len(content)} chars)"


@tool(
    name="edit_file",
    description=(
        "修改已有文件(按行替换)。**调用前必须先 read 或 list_dir 拿到当前内容,严禁凭印象修改。**"
        "mode=edit 时通过 position/end_line 指定替换行范围;"
        "mode=append 时把 content 追加到文件末尾。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string",  "description": "要修改的文件绝对路径"},
            "content":   {"type": "string",  "description": "新内容"},
            "mode":      {"type": "string",  "enum": ["edit", "append"], "default": "edit", "description": "edit=按行替换;append=追加到末尾"},
            "position":  {"type": "integer", "description": "edit 模式的起始行号(0-indexed,包含)"},
            "end_line":  {"type": "integer", "description": "edit 模式的结束行号(不包含),默认 position+1"},
            "auto":      {"type": "boolean", "default": True, "description": "True=立即执行;False=返回方案待确认"},
        },
        "required": ["file_path", "content"],
    },
)
def _edit_file(file_path: str, content: str, mode: str = "edit", position: int | None = None, end_line: int | None = None, auto: bool = True) -> str:
    if not state.get_auto() or not auto:
        pending = {"action": "edit_file", "args": {"file_path": file_path, "content": content, "mode": mode, "position": position, "end_line": end_line}}
        state.set_pending(pending)
        return json.dumps({"status": "pending_approval", **pending}, ensure_ascii=False)
    file.change(file_path, content, mode=mode, position=position, end_line=end_line)
    return f"已修改 {file_path}({mode} 模式,{len(content)} chars)"


@tool(
    name="remove_file",
    description="删除指定文件(不存在/不是文件会报错)",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "要删除的文件绝对路径"},
            "auto":      {"type": "boolean", "default": True, "description": "True=立即执行;False=返回方案待确认"},
        },
        "required": ["file_path"],
    },
)
def _remove_file_tool(file_path: str, auto: bool = True) -> str:
    if not state.get_auto() or not auto:
        pending = {"action": "remove_file", "args": {"file_path": file_path}}
        state.set_pending(pending)
        return json.dumps({"status": "pending_approval", **pending}, ensure_ascii=False)
    file.remove_file(file_path)
    return f"已删除 {file_path}"
