"""
工具注册表与调度层。
- @tool 装饰器:把函数注册为 OpenAI 兼容工具(JSON Schema),同时声明审批策略
- get_tools(plan_model=True) -> list[dict]:返回给模型的 tools 列表
- dispatch(name, args, ctx) -> ToolResult:唯一的执行入口,审批门在这里统一执行
- 审批策略集中在注册表里,不再由各个工具函数手抄:
    mutating=True        需要 auto 或一次性授权才执行,否则写 pending 并返回待确认
    always_confirm=True  无视 auto,必须拿到针对本次调用的一次性授权(run_command)
- 所有路径都先过 sandbox.resolve,越界与受保护文件返回明确错误给模型
"""
import inspect
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

import sandbox
from agent import session as agent_session
from explorer import file


# ──────────────── 读取上限 ────────────────
MAX_READ_LINES = 2000
MAX_READ_BYTES = 100 * 1024

# 这些异常是确定性的,重试同样的参数没有意义
_NO_RETRY_EXC = (
    PermissionError, FileExistsError, FileNotFoundError, NotADirectoryError,
    IsADirectoryError, ValueError, TypeError, KeyError,
)

FILE_TOOLS = frozenset({"create_file", "edit_file", "remove_file"})


# ════════════════════════════════════════════════════════════
#                      结构化返回 & 上下文
# ════════════════════════════════════════════════════════════

@dataclass
class ToolResult:
    """
    工具执行结果。
    ok        是否成功;loop 按这个字段决定重试与事件里的成功标记
    content   喂给模型的文本(tool 消息的 content)
    error     失败原因的机器可读标签,成功时为 None
    retryable 失败是否值得原样重试一次(确定性错误为 False)
    pending   本次调用触发了待确认时的 pending 记录
    """
    ok: bool
    content: str
    error: str | None = None
    retryable: bool = False
    pending: dict | None = None


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的执行上下文,携带会话与本次生效的 auto 值。"""
    session: Any
    auto: bool = False


def context(sid: str | None = None) -> ToolContext:
    """按 sid 构造执行上下文,sid 为空落到默认会话。"""
    s = agent_session.get(sid)
    with s.lock:
        return ToolContext(session=s, auto=bool(s.auto))


# ════════════════════════════════════════════════════════════
#                          注册表
# ════════════════════════════════════════════════════════════

@dataclass
class ToolSpec:
    """一个已注册工具的全部元信息,审批策略也在这里声明。"""
    name: str
    func: Callable
    schema: dict
    mutating: bool = False
    risk: str = "low"
    always_confirm: bool = False
    needs_ctx: bool = False
    approval_keys: tuple = ()


_REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict, *,
         mutating: bool = False, risk: str = "low", always_confirm: bool = False,
         needs_ctx: bool = False, approval_keys: tuple = ()):
    """
    装饰器:把函数注册为 OpenAI 兼容工具。
    mutating       是否会改变外部状态(文件/进程),决定要不要过审批门
    risk           low / medium / high,只用于展示给用户
    always_confirm 无视 auto,每次调用都要用户逐条确认
    needs_ctx      函数第一个参数接收 ToolContext
    approval_keys  判定「一次性授权是否属于本次调用」时要逐字比对的参数名
    """
    def decorator(func: Callable) -> Callable:
        _REGISTRY[name] = ToolSpec(
            name=name,
            func=func,
            schema={
                "type": "function",
                "function": {
                    "name":        name,
                    "description": description,
                    "parameters":  parameters,
                },
            },
            mutating=mutating,
            risk=risk,
            always_confirm=always_confirm,
            needs_ctx=needs_ctx,
            approval_keys=tuple(approval_keys),
        )
        return func
    return decorator


def get_spec(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def tool_names() -> list[str]:
    return list(_REGISTRY)


# ════════════════════════════════════════════════════════════
#                          审批门
# ════════════════════════════════════════════════════════════

def _approval_matches(sess: Any, spec: ToolSpec, args: dict) -> bool:
    """
    判断会话上挂着的一次性授权是不是针对本次调用。
    session.consume_approval 只比对 file_path,这里按工具声明的 approval_keys 再收紧一层,
    避免「确认了命令 A」被拿去执行命令 B。
    """
    a = getattr(sess, "approved", None)
    if not isinstance(a, dict) or a.get("action") != spec.name:
        return False
    want = a.get("args")
    if not isinstance(want, dict):
        want = {}
    for key in spec.approval_keys:
        if want.get(key) != args.get(key):
            return False
    return True


def _authorized(spec: ToolSpec, args: dict, ctx: ToolContext) -> bool:
    """审批门:先看一次性授权,再看 auto;always_confirm 的工具只认一次性授权。"""
    if _approval_matches(ctx.session, spec, args) and ctx.session.consume_approval(spec.name, args):
        return True
    if spec.always_confirm:
        return False
    return bool(ctx.auto)


def _pending_markdown(spec: ToolSpec, args: dict) -> str:
    """生成给用户看的确认卡片正文。"""
    name = spec.name
    if name == "create_file":
        body = args.get("content") or ""
        return f"**创建文件**:`{args.get('file_path', '')}`\n\n初始内容:{len(body)} 字符"
    if name == "edit_file":
        patches = args.get("patches")
        count = len(patches) if isinstance(patches, list) else 0
        return f"**修改文件**:`{args.get('file_path', '')}`\n\n**改动**:{count} 处替换"
    if name == "remove_file":
        return f"**删除文件**:`{args.get('file_path', '')}`"
    if name == "run_command":
        cwd = args.get("cwd") or "默认(用户主目录)"
        return (
            f"**执行命令**(风险:{spec.risk})\n\n"
            f"```sh\n{args.get('command', '')}\n```\n"
            f"工作目录:`{cwd}`\n\n"
            f"超时:{args.get('timeout', 30)} 秒"
        )
    return f"**{name}**\n\n```json\n{json.dumps(args, ensure_ascii=False, indent=2)}\n```"


def _build_pending(spec: ToolSpec, args: dict) -> dict:
    return {
        "action":   spec.name,
        "args":     args,
        "risk":     spec.risk,
        "markdown": _pending_markdown(spec, args),
        "message":  "用户确认后 agent 才会执行这一步;授权只对这一次调用生效。",
    }


# ════════════════════════════════════════════════════════════
#                       工具调用入口
# ════════════════════════════════════════════════════════════

def dispatch(name: str, args: dict | None = None, ctx: ToolContext | None = None) -> ToolResult:
    """
    执行一个已注册工具。审批、参数校验、异常收敛都在这里,任何情况都返回 ToolResult。
    未获授权时写 pending 并原样返回,不执行工具函数。
    """
    spec = _REGISTRY.get(name)
    if spec is None:
        return ToolResult(False, f"错误:未知工具 {name}", error="unknown_tool")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return ToolResult(False, f"错误:工具 {name} 的参数必须是 JSON 对象", error="bad_arguments")
    if ctx is None:
        ctx = context()

    if spec.mutating and not _authorized(spec, args, ctx):
        pending = _build_pending(spec, args)
        ctx.session.set_pending(pending)
        return ToolResult(
            ok=True,
            content=json.dumps({"status": "pending_approval", **pending},
                               ensure_ascii=False, default=str),
            pending=pending,
        )

    call_args = (ctx,) if spec.needs_ctx else ()
    try:
        bound = inspect.signature(spec.func).bind(*call_args, **args)
    except TypeError as e:
        return ToolResult(False, f"错误:工具 {name} 参数不合法:{e}", error="bad_arguments")

    try:
        raw = spec.func(*bound.args, **bound.kwargs)
    except sandbox.SandboxError as e:
        return ToolResult(False, f"错误:{e}", error="sandbox")
    except Exception as e:
        return ToolResult(
            False,
            f"工具 {name} 执行失败:{type(e).__name__}: {e}",
            error=type(e).__name__,
            retryable=not isinstance(e, _NO_RETRY_EXC),
        )

    if isinstance(raw, ToolResult):
        return raw
    return ToolResult(True, "" if raw is None else str(raw))


def call(name: str, sid: str | None = None, **kwargs) -> ToolResult:
    """旧调用点的便利封装:用默认会话跑一次 dispatch,返回 ToolResult。"""
    return dispatch(name, kwargs, context(sid))


# ──────────────── 获取当前可用工具列表 ────────────────
def get_tools(plan_model: bool | None = None, sid: str | None = None) -> list[dict]:
    """
    返回 OpenAI 格式的 tools 列表。
    plan_model=False 时不返回 plan 工具(模型不知道要 plan)。
    """
    if plan_model is None:
        s = agent_session.get(sid)
        with s.lock:
            plan_model = s.plan_model

    out = []
    for name, spec in _REGISTRY.items():
        if name == "plan" and not plan_model:
            continue
        out.append(spec.schema)
    return out


# ════════════════════════════════════════════════════════════
#                        读取辅助
# ════════════════════════════════════════════════════════════

def _truncate_numbered(text: str, start: int) -> tuple[str, str]:
    """
    按行数 / 字节上限截断带行号的读取结果。
    返回 (正文, 截断提示);没截断时提示为空串。
    """
    lines = text.splitlines(keepends=True)
    total = start - 1 + len(lines)
    kept: list[str] = []
    used = 0
    for ln in lines:
        if len(kept) >= MAX_READ_LINES:
            break
        used += len(ln.encode("utf-8", "replace"))
        if used > MAX_READ_BYTES and kept:
            break
        kept.append(ln)
    if len(kept) == len(lines):
        return text, ""
    last = start - 1 + len(kept)
    note = (
        f"\n… 已截断:本次显示第 {start}–{last} 行,文件共 {total} 行。"
        f"继续读取请调用 read_file(start_line={last + 1})"
    )
    return "".join(kept), note


def _pending_patch_notice(store: Any, path: str) -> str:
    """
    该文件有未确认 patch 时,给模型一段说明。
    这里不返回 store.get_preview() 的预览全文:store_patch 每次都拿磁盘内容当基线,
    模型若照着预览写 old 会匹配不上,且新预览会覆盖上一份未确认的改动。
    """
    try:
        if not store.has_pending(path):
            return ""
        d = store.get_diff(path) or {}
    except Exception:
        return ""
    lines = d.get("lines") or []
    added = sum(1 for ln in lines if ln.get("type") == "add")
    deleted = sum(1 for ln in lines if ln.get("type") == "del")
    return (
        f"[注意] 该文件有一份尚未确认的 patch(+{added} / -{deleted} 行),还没有写入磁盘。"
        f"下面显示的是磁盘上的当前内容,再次 edit_file 时 old 必须以磁盘内容为准;"
        f"同一文件再次 edit_file 会基于磁盘重新生成预览,覆盖这份未确认的改动。\n\n"
    )


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
    needs_ctx=True,
)
def _plan_tool(ctx: ToolContext, intent: str, direction: str, basis: str,
               affected_files: list, steps: list, risk: str) -> ToolResult:
    files = [str(f) for f in (affected_files or [])]
    items = [str(s) for s in (steps or [])]
    plan = {
        "intent": intent, "direction": direction, "basis": basis,
        "affected_files": files, "steps": items, "risk": risk,
    }
    md = (
        f"## 📋 方案确认\n\n"
        f"**目标**: {intent}\n\n"
        f"**方向**: {direction}\n\n"
        f"**依据**: {basis}\n\n"
        f"**涉及文件**:\n" + "\n".join(f"- `{f}`" for f in files) + "\n\n"
        f"**步骤**:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(items)) + "\n\n"
        f"**风险**: {risk}\n"
    )
    pending = {
        "action":   "plan",
        "args":     plan,
        "risk":     risk,
        "markdown": md,
        "message":  "用户在 chat 中确认后,agent 才会继续。",
    }
    ctx.session.set_pending(pending)
    return ToolResult(ok=True, content=md, pending=pending)


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
    full = sandbox.resolve(path)
    result = file.list_dir(full, show_hidden=show_hidden)
    return json.dumps(result, ensure_ascii=False, indent=2)


@tool(
    name="read_file",
    description=(
        "按行号读取文件内容(行号从 1 开始)。start_line 缺省则从第 1 行读到末尾。"
        f"单次最多返回 {MAX_READ_LINES} 行 / {MAX_READ_BYTES // 1024} KB,超出会截断并提示如何续读。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path":       {"type": "string",  "description": "文件绝对路径"},
            "start_line": {"type": "integer", "description": "起始行号(1-indexed,包含)"},
        },
        "required": ["path"],
    },
    needs_ctx=True,
)
def _read_file_tool(ctx: ToolContext, path: str, start_line: int | None = None) -> str:
    full = sandbox.resolve(path)
    start = 1 if start_line is None else max(1, int(start_line))
    body, note = _truncate_numbered(file.read(full, start_line=start), start)
    return _pending_patch_notice(ctx.session.diffs, full) + body + note


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
        },
        "required": ["file_path"],
    },
    mutating=True,
    risk="medium",
    needs_ctx=True,
    approval_keys=("file_path",),
)
def _create_file(ctx: ToolContext, file_path: str, content: str = "") -> ToolResult:
    full = sandbox.resolve(file_path, write=True, agent=True)
    if os.path.exists(full):
        return ToolResult(
            False,
            f"文件已存在,禁止覆盖: {full}。如需修改请用 edit_file",
            error="file_exists",
        )
    body = content or ""
    store = ctx.session.diffs
    store.snapshot_before(full)
    file.create(full, body, agent=True)
    store.snapshot_after(full, "create")
    return ToolResult(True, f"已创建 {full}({len(body)} chars)")


@tool(
    name="edit_file",
    description=(
        "修改已有文件(搜索替换 patch 模式)。**调用前必须先 read_file 拿到当前内容,严禁凭印象修改。**\n"
        "传入 patches 数组,每个 patch 包含 old(要替换的原文,必须和文件中的内容完全一致,含缩进)和 new(替换后的内容)。\n"
        "old 必须在文件中唯一匹配,包含足够上下文确保唯一性。\n"
        "改动不会立即写入磁盘,会先在前端显示 diff,用户确认保留后才生效。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string",  "description": "要修改的文件绝对路径"},
            "patches":   {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old": {"type": "string", "description": "要替换的原文本(必须和文件内容完全一致,含缩进和换行)"},
                        "new": {"type": "string", "description": "替换后的新文本"},
                    },
                    "required": ["old", "new"],
                },
                "description": "搜索替换 patch 列表",
            },
        },
        "required": ["file_path", "patches"],
    },
    mutating=True,
    risk="medium",
    needs_ctx=True,
    approval_keys=("file_path",),
)
def _edit_file(ctx: ToolContext, file_path: str, patches: list) -> ToolResult:
    if not isinstance(patches, list) or not patches:
        return ToolResult(False, "错误:patches 必须是非空数组", error="bad_arguments")
    full = sandbox.resolve(file_path, write=True, agent=True)
    result = ctx.session.diffs.store_patch(full, patches)
    if not result.get("ok"):
        return ToolResult(
            False,
            f"edit_file 失败:{result.get('error', '未知错误')}",
            error="patch_failed",
        )
    return ToolResult(
        True,
        f"已生成 patch 预览({len(patches)} 处改动),等待用户在前端确认保留或撤销",
    )


@tool(
    name="remove_file",
    description="删除指定文件(不存在/不是文件会报错)",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "要删除的文件绝对路径"},
        },
        "required": ["file_path"],
    },
    mutating=True,
    risk="high",
    needs_ctx=True,
    approval_keys=("file_path",),
)
def _remove_file_tool(ctx: ToolContext, file_path: str) -> ToolResult:
    full = sandbox.resolve(file_path, write=True, agent=True)
    store = ctx.session.diffs
    store.snapshot_before(full)
    file.remove_file(full, agent=True)
    store.snapshot_after(full, "remove")
    return ToolResult(True, f"已删除 {full}")


@tool(
    name="run_command",
    description=(
        "在 shell 中执行一条命令并返回 stdout / stderr / returncode。"
        "适用于运行脚本、跑测试、git/pip/node 等命令行工具。"
        "长时间运行的命令请传 timeout(秒)。"
        "每次调用都需要用户逐条确认,确认后必须用完全相同的参数再调用一次。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string",  "description": "要执行的 shell 命令"},
            "cwd":     {"type": "string",  "description": "工作目录(绝对路径,默认用户主目录)"},
            "timeout": {"type": "integer", "default": 30, "description": "超时秒数(默认 30)"},
        },
        "required": ["command"],
    },
    mutating=True,
    risk="high",
    always_confirm=True,
    approval_keys=("command", "cwd"),
)
def _run_command_tool(command: str, cwd: str | None = None, timeout: int = 30) -> ToolResult:
    from terminal import run as term_run
    result = term_run(command, cwd=cwd, timeout=timeout)
    parts = [
        f"command:   {result.get('command', '')}",
        f"cwd:       {result.get('cwd', '')}",
        f"returncode: {result.get('returncode', '?')}",
    ]
    out = (result.get("stdout") or "").strip()
    err = (result.get("stderr") or "").strip()
    if out:
        parts.append("--- stdout ---\n" + out)
    if err:
        parts.append("--- stderr ---\n" + err)
    if result.get("error"):
        parts.append("error: " + str(result["error"]))
    if result.get("truncated"):
        parts.append("(输出被截断)")
    ok = bool(result.get("ok"))
    return ToolResult(
        ok=ok,
        content="\n".join(parts),
        error=None if ok else (result.get("error") or f"returncode={result.get('returncode')}"),
    )
