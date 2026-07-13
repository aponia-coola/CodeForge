import json
import os
import re
import threading
import time
from pathlib import Path
from openai import OpenAI

_CONFIG_PATH = Path(__file__).resolve().parent / "model.json"
_CONFIG: dict = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))

# ===== CodeBuddy 兼容 schema 解析 =====
# 顶层结构(参考 ~/.codebuddy/models.json):
#   models: [{ id, name, vendor, apiKey, url, maxInputTokens, maxOutputTokens,
#             supportsToolCall, supportsImages, supportsReasoning }, ...]
#   availableModels: ["id1", "id2", ...]   # 可选白名单
#   current_model: "..."                   # 顶层字段(本地扩展,CodeBuddy 会忽略)


def _strip_chat_completions(url: str) -> str:
    """url 形如 https://host/v1/chat/completions → OpenAI 需要 base_url=https://host/v1"""
    if not url:
        return url
    return re.sub(r"/chat/completions/?$", "", url.rstrip("/"))


def _parse_config(cfg: dict) -> dict:
    """
    把 CodeBuddy 风格(或旧格式)配置归一化成内部状态。
    Returns: {"current_model", "models": [...], "available": set|None}
    """
    models = list(cfg.get("models", []))
    # 旧格式兼容:顶层 base_url + models 只有 id/name → 自动补 url/apiKey
    legacy_base = cfg.get("base_url")
    legacy_key = cfg.get("api_key")
    for m in models:
        if "url" not in m and legacy_base:
            m["url"] = legacy_base.rstrip("/") + "/chat/completions"
        if "apiKey" not in m and legacy_key:
            m["apiKey"] = legacy_key
    available = cfg.get("availableModels")
    available_set = set(available) if isinstance(available, list) and available else None
    current = cfg.get("current_model")
    if not current:
        if available:
            current = available[0]
        elif models:
            current = models[0].get("id")
    return {"current_model": current, "models": models, "available": available_set}


_PARSED = _parse_config(_CONFIG)
_CURRENT_MODEL: str = _PARSED["current_model"]
_AVAILABLE = _PARSED["available"]


def _find_model(model_id: str) -> dict | None:
    for m in _PARSED["models"]:
        if m.get("id") == model_id:
            return m
    return None


def _current_base_url() -> str:
    m = _find_model(_CURRENT_MODEL)
    if m and m.get("url"):
        return _strip_chat_completions(m["url"])
    return _CONFIG.get("base_url", "") or ""


def _current_api_key() -> str:
    m = _find_model(_CURRENT_MODEL)
    if m:
        return m.get("apiKey") or m.get("api_key") or ""
    return _CONFIG.get("api_key", "") or ""


_BASE_URL: str = _current_base_url()
_API_KEY: str = _current_api_key()


def _is_minimax() -> bool:
    """当前模型是否 MiniMax 家族。MiniMax 才支持 reasoning_split 私有扩展。"""
    return 'minimax' in (_CURRENT_MODEL or '').lower()


def _filtered_models() -> list[dict]:
    """返回前端可见的模型简表 [{id, name}],遵守 availableModels 白名单。"""
    out = []
    for m in _PARSED["models"]:
        if _AVAILABLE is not None and m.get("id") not in _AVAILABLE:
            continue
        out.append({"id": m.get("id", ""), "name": m.get("name") or m.get("id", "")})
    return out


_MODELS: list[dict] = _filtered_models()


def _build_client() -> OpenAI:
    return OpenAI(api_key=_API_KEY, base_url=_BASE_URL or None)


_client: OpenAI = _build_client()


def get_current_model() -> str:
    """返回当前生效的模型 id。"""
    return _CURRENT_MODEL


def get_base_url() -> str:
    """返回当前生效的 base_url。"""
    return _BASE_URL


def get_models() -> list[dict]:
    """
    返回模型列表: [{"id": "...", "name": "..."}, ...]
    id  用于 API 调用,name 用于界面显示。
    """
    return _MODELS


def set_current_model(model_id: str) -> bool:
    """
    热切换当前模型。会同步重建 OpenAI client(不同模型可能 base_url/apiKey 不同),
    并把 current_model 持久化回 model.json,这样 reload 时不会被覆盖回旧值。
    Returns: 切换成功返回 True,id 不存在或在白名单外返回 False。
    """
    global _CURRENT_MODEL, _BASE_URL, _API_KEY, _client
    m = _find_model(model_id)
    if not m:
        return False
    if _AVAILABLE is not None and model_id not in _AVAILABLE:
        return False
    _CURRENT_MODEL = model_id
    _BASE_URL = _strip_chat_completions(m.get("url", "")) or _BASE_URL
    _API_KEY = m.get("apiKey") or m.get("api_key") or _API_KEY
    _client = _build_client()
    # 持久化:把 current_model 写回 model.json,保证 reload 后仍是用户的选择
    try:
        cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        cfg["current_model"] = model_id
        _CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _CONFIG["current_model"] = model_id
    except Exception:
        pass
    return True


def reload_config(config_path: str | os.PathLike | None = None) -> dict:
    """
    重新读取 model.json 并更新所有配置,实现热切换。
    Args:
        config_path: 可选,指定新的 model.json 路径;None 则使用模块默认路径
    Returns:
        切换后生效的完整配置 {"current_model": ..., "base_url": ..., "models": [...]}
    """
    global _CONFIG, _PARSED, _CURRENT_MODEL, _AVAILABLE, _BASE_URL, _API_KEY, _MODELS, _client
    path = Path(config_path) if config_path else _CONFIG_PATH
    _CONFIG = json.loads(path.read_text(encoding="utf-8"))
    _PARSED = _parse_config(_CONFIG)
    _CURRENT_MODEL = _PARSED["current_model"]
    _AVAILABLE = _PARSED["available"]
    _BASE_URL = _current_base_url()
    _API_KEY = _current_api_key()
    _MODELS = _filtered_models()
    _client = _build_client()
    return {
        "current_model": _CURRENT_MODEL,
        "base_url": _BASE_URL,
        "models": _MODELS,
    }


def reload_models_list() -> None:
    """
    只刷新模型列表(支持新增/删除),不动当前 current_model / base_url / apiKey。
    适合 /api/models 这种前端定期拉取模型列表的场景,避免覆盖用户刚切换的模型。
    """
    global _CONFIG, _PARSED, _AVAILABLE, _MODELS
    _CONFIG = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    _PARSED = _parse_config(_CONFIG)
    _AVAILABLE = _PARSED["available"]
    _MODELS = _filtered_models()


def request(
    message: str | None = None,
    messages: list | None = None,
    tools: list | None = None,
    use_thinking: bool = True,
):
    """
    调用模型,支持工具调用。工具列表默认从 agent.tool._TOOLS 复用。
    返回 message 对象(不是字符串),无工具时用 .content 拿正文,有工具时检查 .tool_calls。
    """
    # 1. 构造 messages
    if messages is None:
        if message is None:
            raise ValueError("必须传 message 或 messages")
        messages = [
            {'role': 'system', 'content': 'You named CodeForge, an AI Agent IDE on Android Termux.'},
            {"role": "user", "content": message},
        ]

    # 2. 工具列表(默认从 agent.tool 拿,懒加载避免循环 import)
    if tools is None:
        from agent.tool import _TOOLS
        tools = _TOOLS or None

    # 3. 构造请求
    kwargs = dict(model=_CURRENT_MODEL, messages=messages, stream=False)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if use_thinking and _is_minimax():
        kwargs["extra_body"] = {"reasoning_split": True}

    return _client.chat.completions.create(**kwargs).choices[0].message


# ──────────── 流式输出(MiniMax / OpenAI 兼容) ────────────

class _StreamMsg:
    """把流式 chunk 累积还原成 message 形状,供 loop 后续判断 tool_calls。"""
    def __init__(self, content, reasoning, tool_calls):
        self.content = content
        self.reasoning_content = reasoning
        self.tool_calls = tool_calls  # list 或 None


def request_stream(
    messages: list,
    tools: list | None = None,
    use_thinking: bool = True,
):
    """
    流式生成器,逐步 yield 事件:
      {'type': 'reasoning', 'text': '...'}
      {'type': 'content',   'text': '...'}
      {'type': 'done',      'message': <_StreamMsg>}
    与 request() 输出语义一致,只是边收边推。
    """
    kwargs = dict(model=_CURRENT_MODEL, messages=messages, stream=True)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if use_thinking and _is_minimax():
        kwargs["extra_body"] = {"reasoning_split": True}

    stream = _client.chat.completions.create(**kwargs)

    content_parts: list[str]   = []
    reasoning_parts: list[str] = []
    tool_calls_map: dict[int, dict] = {}

    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta

        # 思考过程(MiniMax reasoning_split 扩展)
        r = getattr(delta, "reasoning_content", None)
        if r:
            reasoning_parts.append(r)
            yield {"type": "reasoning", "text": r}

        # 实际正文
        c = getattr(delta, "content", None)
        if c:
            content_parts.append(c)
            yield {"type": "content", "text": c}

        # 工具调用增量:按 index 累积
        tcs = getattr(delta, "tool_calls", None)
        if tcs:
            for tc in tcs:
                idx = getattr(tc, "index", None)
                if idx is None:
                    continue
                slot = tool_calls_map.setdefault(idx, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn:
                    if getattr(fn, "name", None):
                        slot["function"]["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["function"]["arguments"] += fn.arguments

    # 还原成与 request() 兼容的 message 形状
    class _TC:
        def __init__(self, d):
            self.id       = d["id"]
            self.type     = d["type"]
            self.function = type("F", (), {
                "name":      d["function"]["name"],
                "arguments": d["function"]["arguments"],
            })()
    tool_calls = [_TC(tool_calls_map[i]) for i in sorted(tool_calls_map)]
    yield {
        "type": "done",
        "message": _StreamMsg(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls or None,
        ),
    }


# ============================================================================
#  热更新:监听 model.json 文件变化,自动 reload_config()
# ----------------------------------------------------------------------------
#  设计要点:
#   1. watchdog 不可用 → 静默退化,只支持 /api/models 手动 reload
#   2. 防抖 250ms:VSCode/Vim 一次保存会触发多个事件(modify + modify + ...)
#   3. 监听父目录再过滤文件名:有些编辑器是"写到 tmp + rename",
#      直接监听文件路径在 rename 那一刻会丢失
#   4. 后台 daemon 线程,进程退出时随主线程一起死
# ============================================================================
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    _HAS_WATCHDOG = True
except Exception:  # ImportError / 平台不支持
    _HAS_WATCHDOG = False

_WATCH_DEBOUNCE = 0.25   # 秒
_watcher_started = False
_watcher_lock = threading.Lock()


class _ModelFileHandler(FileSystemEventHandler if _HAS_WATCHDOG else object):
    """model.json 改动后,debounce 再 reload。"""
    def __init__(self, target: Path):
        self._target = target.resolve()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _is_target(self, event: "FileSystemEvent") -> bool:
        try:
            p = Path(getattr(event, "dest_path", None) or event.src_path).resolve()
        except Exception:
            return False
        return p == self._target

    def _trigger(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_WATCH_DEBOUNCE, _safe_reload)
            self._timer.daemon = True
            self._timer.start()

    if _HAS_WATCHDOG:
        def on_modified(self, event):
            if not event.is_directory and self._is_target(event):
                self._trigger()
        def on_created(self, event):
            if not event.is_directory and self._is_target(event):
                self._trigger()
        def on_moved(self, event):
            # 编辑器常用 "写到 tmp → rename" 的原子保存
            if not event.is_directory and self._is_target(event):
                self._trigger()


def _safe_reload():
    """线程安全的 reload,出错只打日志不抛。"""
    try:
        cfg = reload_config()
        print(f"[models] hot-reloaded: current={cfg.get('current_model')!r} "
              f"models={len(cfg.get('models', []))}", flush=True)
    except Exception as e:
        print(f"[models] hot-reload failed: {type(e).__name__}: {e}", flush=True)


def start_watcher(daemon: bool = True) -> bool:
    """
    启动 model.json 文件监听(只启动一次,重复调用安全)。
    Returns: True 表示监听已就绪,False 表示退化(无 watchdog 或启动失败)。
    """
    global _watcher_started
    with _watcher_lock:
        if _watcher_started:
            return True
        if not _HAS_WATCHDOG:
            return False
        try:
            watch_dir = _CONFIG_PATH.resolve().parent
            handler = _ModelFileHandler(_CONFIG_PATH)
            obs = Observer()
            obs.schedule(handler, str(watch_dir), recursive=False)
            obs.daemon = daemon
            obs.start()
            _watcher_started = True
            print(f"[models] watching {_CONFIG_PATH} (debounce {_WATCH_DEBOUNCE}s)", flush=True)
            return True
        except Exception as e:
            print(f"[models] watcher start failed: {type(e).__name__}: {e}", flush=True)
            return False


# 模块导入即启动(失败也不影响主流程)
start_watcher()
