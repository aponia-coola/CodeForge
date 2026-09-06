"""
模型请求层:配置加载、模型热切换、同步/流式调用。

配置分两层:
  - models/model.json        进 git 的基础清单,本模块只读不写(历史上的明文 key 仍能用)
  - models/model.local.json  未跟踪的本地覆盖层,结构与上面完全一致,以 id 为键覆盖合并

key 解析优先级(高到低):
  1. 条目 apiKeyEnv 指定的环境变量
  2. 本地覆盖层同 id 条目的 apiKey
  3. model.json 条目的 apiKey(向后兼容)

线程安全约定:
  - 所有模块级可变状态由 _STATE_LOCK 保护(请求线程 / watchdog 定时器线程并发访问)
  - 请求方通过 _snapshot() 一次性取走不可变快照,请求过程中不会被 reload 撕裂
  - 本地层写回走"临时文件 + os.replace",并临时抑制 watcher 避免自触发 reload
"""
import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

_CONFIG_PATH = Path(__file__).resolve().parent / "model.json"
_LOCAL_FILENAME = "model.local.json"
_LOCAL_PATH = _CONFIG_PATH.with_name(_LOCAL_FILENAME)

_DEFAULT_TIMEOUT = 120.0
_DEFAULT_RETRIES = 2
_RETRY_BASE_DELAY = 0.5
_RETRY_MAX_DELAY = 8.0
_RETRY_AFTER_CAP = 30.0
_ENV_TIMEOUT = "CODEFORGE_MODEL_TIMEOUT"
_ENV_RETRIES = "CODEFORGE_MODEL_RETRIES"
_TEST_TIMEOUT = 20.0
_MISSING_KEY = "codeforge-missing-api-key"

_WATCH_DEBOUNCE = 0.25
_WATCH_SUPPRESS = 1.0


# ════════════════════════════════════════════════════════════
#                  CodeBuddy 兼容 schema 解析
# ════════════════════════════════════════════════════════════
# 顶层结构(参考 ~/.codebuddy/models.json):
#   models: [{ id, name, vendor, apiKey, url, maxInputTokens, maxOutputTokens,
#             supportsToolCall, supportsImages, supportsReasoning }, ...]
#   availableModels: ["id1", "id2", ...]   可选白名单
#   current_model: "..."                   顶层字段(本地扩展,CodeBuddy 会忽略)


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


def _find_model(parsed: dict, model_id: str | None) -> dict | None:
    """在已解析的配置里按 id 找模型条目。"""
    for m in parsed.get("models", []):
        if m.get("id") == model_id:
            return m
    return None


def _pick_base_url(cfg: dict, entry: dict | None) -> str:
    """模型条目的 url 优先,回退到顶层 base_url(旧格式)。"""
    if entry and entry.get("url"):
        return _strip_chat_completions(entry["url"])
    return cfg.get("base_url", "") or ""


def _env_key_name(entry: dict | None) -> str:
    """条目声明的环境变量名,没写或类型不对返回空串。"""
    if not entry:
        return ""
    name = entry.get("apiKeyEnv") or entry.get("api_key_env") or ""
    return name.strip() if isinstance(name, str) else ""


def _pick_api_key(cfg: dict, entry: dict | None) -> str:
    """
    解析条目实际使用的 key。
    apiKeyEnv 指向的环境变量最优先;其次是条目自身的 apiKey
    (本地覆盖层已在合并阶段盖过 model.json);最后回退顶层 api_key(旧格式)。
    """
    if entry:
        name = _env_key_name(entry)
        if name:
            value = os.environ.get(name)
            if value:
                return value
        return entry.get("apiKey") or entry.get("api_key") or ""
    return cfg.get("api_key", "") or ""


def _filtered_models(parsed: dict) -> list[dict]:
    """返回前端可见的模型简表 [{id, name}],遵守 availableModels 白名单。"""
    available = parsed.get("available")
    out = []
    for m in parsed.get("models", []):
        if available is not None and m.get("id") not in available:
            continue
        out.append({"id": m.get("id", ""), "name": m.get("name") or m.get("id", "")})
    return out


def _sniff_reasoning(model_id: str | None) -> bool:
    """回退嗅探:MiniMax 家族才支持 reasoning_split 私有扩展。"""
    return "minimax" in (model_id or "").lower()


def _supports_reasoning(entry: dict | None, model_id: str | None) -> bool:
    """
    是否给该模型发 reasoning_split。
    优先读 model.json 条目里的显式能力字段(supportsReasoning / supports_reasoning),
    字段缺失时才回退到按模型 id 嗅探。
    """
    for key in ("supportsReasoning", "supports_reasoning"):
        if entry and key in entry:
            return bool(entry[key])
    return _sniff_reasoning(model_id)


# ════════════════════════════════════════════════════════════
#                 本地覆盖层(models/model.local.json)
# ════════════════════════════════════════════════════════════
# 结构与 model.json 完全一致:{models: [...], current_model, availableModels}
# 合并规则:
#   - models 以 id 为键,本地条目逐字段覆盖同 id 的跟踪条目,新 id 追加到表尾
#   - 顶层字段(current_model / availableModels / timeout ...)本地写了就盖住跟踪层
#   - 文件不存在、为空或坏掉,一切按只有 model.json 的现状工作


def _local_path_for(tracked_path: Path) -> Path:
    """给定跟踪层路径,推出同目录的本地覆盖层路径。"""
    if tracked_path == _CONFIG_PATH:
        return _LOCAL_PATH
    return tracked_path.with_name(_LOCAL_FILENAME)


def _read_local(path: Path) -> tuple[dict, str]:
    """
    读本地覆盖层,返回 (文档, 原始文本)。
    文件缺失、非法 JSON、顶层不是对象都退化成空文档,坏掉的本地文件不会让模型层起不来。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    try:
        doc = json.loads(raw)
    except ValueError as e:
        print(f"[models] {path.name} 解析失败,已忽略: {e}", flush=True)
        return {}, raw
    if not isinstance(doc, dict):
        print(f"[models] {path.name} 顶层不是对象,已忽略", flush=True)
        return {}, raw
    return doc, raw


def _has_key(entry: dict | None) -> bool:
    """该层条目里是否写了非空 apiKey。"""
    if not entry:
        return False
    return bool(entry.get("apiKey") or entry.get("api_key"))


def _key_source(entry: dict | None, base: str) -> str:
    """在"key 来自哪一层"的基线上叠加环境变量分支,返回最终来源。"""
    name = _env_key_name(entry)
    if name and os.environ.get(name):
        return "env"
    return base or "none"


def _merge_models(tracked: list | None, local: list | None) -> tuple[list, dict, dict]:
    """
    以 id 为键覆盖合并两层模型表。
    Returns: (合并后的模型表, {id: origin}, {id: key 基线来源})
    origin 取 'local'/'tracked',key 基线取 'local'/'tracked'/'none'(env 在读取时叠加)。
    """
    merged: list[dict] = []
    index: dict[str, int] = {}
    origins: dict[str, str] = {}
    key_bases: dict[str, str] = {}
    for m in tracked or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        mid = m["id"]
        index[mid] = len(merged)
        merged.append(dict(m))
        origins[mid] = "tracked"
        key_bases[mid] = "tracked" if _has_key(m) else "none"
    for m in local or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        mid = m["id"]
        overlay = {k: v for k, v in m.items() if v is not None}
        if mid in index:
            merged[index[mid]].update(overlay)
        else:
            index[mid] = len(merged)
            merged.append(dict(overlay))
        origins[mid] = "local"
        if _has_key(m):
            key_bases[mid] = "local"
        else:
            key_bases.setdefault(mid, "none")
    return merged, origins, key_bases


@dataclass(frozen=True)
class _Layers:
    """两层配置合成后的结果,连同溯源信息一起交给安装函数。"""
    cfg: dict
    raw: str
    local_raw: str
    origins: dict
    key_bases: dict
    tracked_ids: frozenset


def _compose(tracked: dict, local: dict) -> tuple[dict, dict, dict]:
    """把跟踪层与本地层合成一份完整配置,并给出 origin / key 基线映射。"""
    models, origins, key_bases = _merge_models(tracked.get("models"), local.get("models"))
    cfg = {k: v for k, v in tracked.items() if k != "models"}
    for k, v in local.items():
        if k == "models" or v is None:
            continue
        cfg[k] = v
    cfg["models"] = models
    current = local.get("current_model")
    if current and current not in {m.get("id") for m in models}:
        cfg["current_model"] = tracked.get("current_model")
    return cfg, origins, key_bases


def _load_layers(tracked_path: Path) -> _Layers:
    """读跟踪层 + 本地层并合成。跟踪层缺失或非法照旧抛错,由调用方决定怎么处理。"""
    raw = tracked_path.read_text(encoding="utf-8")
    tracked = json.loads(raw)
    if not isinstance(tracked, dict):
        raise ValueError(f"{tracked_path.name} 顶层不是对象")
    local, local_raw = _read_local(_local_path_for(tracked_path))
    cfg, origins, key_bases = _compose(tracked, local)
    tracked_ids = frozenset(
        m["id"] for m in tracked.get("models") or []
        if isinstance(m, dict) and m.get("id")
    )
    return _Layers(cfg, raw, local_raw, origins, key_bases, tracked_ids)


# ════════════════════════════════════════════════════════════
#                      超时 / 重试参数解析
# ════════════════════════════════════════════════════════════

def _positive_float(value) -> float | None:
    """把配置值转成正浮点数,非法或非正数返回 None。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _non_negative_int(value) -> int | None:
    """把配置值转成非负整数,非法返回 None。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _resolve_timeout(entry: dict | None, cfg: dict) -> float:
    """
    单次请求超时(秒)。
    优先级:模型条目 timeout/requestTimeout > 顶层同名字段 > 环境变量
    CODEFORGE_MODEL_TIMEOUT > 默认 120 秒。
    """
    for src in (entry or {}, cfg):
        for key in ("timeout", "requestTimeout"):
            v = _positive_float(src.get(key))
            if v is not None:
                return v
    return _positive_float(os.environ.get(_ENV_TIMEOUT)) or _DEFAULT_TIMEOUT


def _resolve_retries(entry: dict | None, cfg: dict) -> int:
    """
    可重试错误的最大重试次数(不含首次调用)。
    优先级:模型条目 maxRetries/retries > 顶层同名字段 > 环境变量
    CODEFORGE_MODEL_RETRIES > 默认 2 次。
    """
    for src in (entry or {}, cfg):
        for key in ("maxRetries", "retries"):
            v = _non_negative_int(src.get(key))
            if v is not None:
                return v
    env = _non_negative_int(os.environ.get(_ENV_RETRIES))
    return _DEFAULT_RETRIES if env is None else env


# ════════════════════════════════════════════════════════════
#                    全局状态(_STATE_LOCK 保护)
# ════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ModelSnapshot:
    """
    当前模型的只读快照。
    请求方一次性取走,后续 reload / set_current_model 不会影响进行中的请求。
    """
    model_id: str
    base_url: str
    supports_reasoning: bool
    timeout: float
    max_retries: int
    api_key: str = field(repr=False)
    client: OpenAI = field(repr=False)


_STATE_LOCK = threading.RLock()

_CONFIG: dict = {}
_PARSED: dict = {"current_model": None, "models": [], "available": None}
_AVAILABLE: set | None = None
_MODELS: list[dict] = []
_CURRENT_MODEL: str = ""
_BASE_URL: str = ""
_API_KEY: str = ""
_client: OpenAI | None = None
_SNAPSHOT: ModelSnapshot | None = None
_LAST_RAW: str = ""
_LOCAL_RAW: str = ""
_ORIGINS: dict[str, str] = {}
_KEY_BASES: dict[str, str] = {}
_TRACKED_IDS: frozenset = frozenset()
_SUPPRESS_UNTIL: float = 0.0


def _build_client(api_key: str, base_url: str, timeout: float) -> OpenAI:
    """
    构造 OpenAI client。
    max_retries=0:重试策略由本模块的退避逻辑统一负责,避免和 SDK 内置重试相乘。
    key 为空时塞占位串:SDK 拿到空串会直接抛,那会让"key 只放环境变量而变量没设"
    的条目在导入期就炸掉整个模型层;塞占位串则退化成请求时的 401,可诊断得多。
    """
    return OpenAI(
        api_key=api_key or _MISSING_KEY,
        base_url=base_url or None,
        timeout=timeout,
        max_retries=0,
    )


def _install_current(model_id: str | None) -> None:
    """锁内切换当前模型:重算 base_url / apiKey,重建 client 与快照。"""
    global _CURRENT_MODEL, _BASE_URL, _API_KEY, _client, _SNAPSHOT
    entry = _find_model(_PARSED, model_id)
    timeout = _resolve_timeout(entry, _CONFIG)
    _CURRENT_MODEL = model_id or ""
    _BASE_URL = _pick_base_url(_CONFIG, entry)
    _API_KEY = _pick_api_key(_CONFIG, entry)
    _client = _build_client(_API_KEY, _BASE_URL, timeout)
    _SNAPSHOT = ModelSnapshot(
        model_id=_CURRENT_MODEL,
        base_url=_BASE_URL,
        supports_reasoning=_supports_reasoning(entry, _CURRENT_MODEL),
        timeout=timeout,
        max_retries=_resolve_retries(entry, _CONFIG),
        api_key=_API_KEY,
        client=_client,
    )


def _install_config(cfg: dict, raw: str | None = None, current: str | None = None) -> None:
    """锁内安装一份新配置:重新解析模型表,再落地当前模型。"""
    global _CONFIG, _PARSED, _AVAILABLE, _MODELS, _LAST_RAW
    _CONFIG = cfg
    _PARSED = _parse_config(cfg)
    _AVAILABLE = _PARSED["available"]
    _MODELS = _filtered_models(_PARSED)
    if raw is not None:
        _LAST_RAW = raw
    _install_current(current or _PARSED["current_model"])


def _install_layers(layers: _Layers, current: str | None = None) -> None:
    """锁内安装两层合成结果:先落地溯源映射,再走通用的配置安装。"""
    global _ORIGINS, _KEY_BASES, _TRACKED_IDS, _LOCAL_RAW
    _ORIGINS = layers.origins
    _KEY_BASES = layers.key_bases
    _TRACKED_IDS = layers.tracked_ids
    _LOCAL_RAW = layers.local_raw
    _install_config(layers.cfg, layers.raw, current)


def _reload_locked() -> None:
    """锁内重新合成两层配置,当前模型仍存在就保持不变,否则回退到配置里的默认值。"""
    layers = _load_layers(_CONFIG_PATH)
    ids = {m.get("id") for m in layers.cfg.get("models", [])}
    _install_layers(layers, _CURRENT_MODEL if _CURRENT_MODEL in ids else None)


def _suppress_watch(seconds: float = _WATCH_SUPPRESS) -> None:
    """本进程写配置文件之后临时抑制 watcher,避免自触发 reload 风暴。"""
    global _SUPPRESS_UNTIL
    with _STATE_LOCK:
        _SUPPRESS_UNTIL = time.monotonic() + seconds


def _watch_suppressed() -> bool:
    """当前是否处于 watcher 抑制窗口内。"""
    with _STATE_LOCK:
        return time.monotonic() < _SUPPRESS_UNTIL


def _snapshot() -> ModelSnapshot:
    """原子取走当前模型的完整快照。"""
    with _STATE_LOCK:
        if _SNAPSHOT is None:
            _install_current(_PARSED.get("current_model"))
        return _SNAPSHOT


def _bootstrap() -> None:
    """模块导入时加载一次 model.json,并叠加本地覆盖层。"""
    layers = _load_layers(_CONFIG_PATH)
    with _STATE_LOCK:
        _install_layers(layers)


_bootstrap()


# ════════════════════════════════════════════════════════════
#                          对外访问器
# ════════════════════════════════════════════════════════════

def get_current_model() -> str:
    """返回当前生效的模型 id。"""
    with _STATE_LOCK:
        return _CURRENT_MODEL


def get_base_url() -> str:
    """返回当前生效的 base_url。"""
    with _STATE_LOCK:
        return _BASE_URL


def get_models() -> list[dict]:
    """
    返回模型列表: [{"id": "...", "name": "..."}, ...]
    id  用于 API 调用,name 用于界面显示。
    返回的是副本,调用方改它不会污染模块状态。
    """
    with _STATE_LOCK:
        return [dict(m) for m in _MODELS]


def current_snapshot() -> ModelSnapshot:
    """返回当前模型的只读快照(model_id / base_url / 超时 / 重试次数等)。"""
    return _snapshot()


# ════════════════════════════════════════════════════════════
#                     持久化 & 配置热重载
# ════════════════════════════════════════════════════════════

def _atomic_write_json(path: Path, data: dict) -> str:
    """
    原子写回 JSON:先写同目录临时文件并 fsync,再 os.replace 覆盖目标。
    中途失败只会留下被清掉的临时文件,目标文件要么是旧内容要么是新内容,
    watcher 或其它进程不可能读到半截文件。返回实际写入的文本。
    """
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return text


def _save_local(doc: dict) -> None:
    """
    原子写本地覆盖层(锁内调用)。
    抑制窗口在写前打开、写后重新计时(fsync 本身可能就耗掉一整个窗口),
    再叠加"把写出去的文本同步进 _LOCAL_RAW",两层保证 watcher 不会被自己的写触发。
    """
    global _LOCAL_RAW
    _suppress_watch()
    text = _atomic_write_json(_LOCAL_PATH, doc)
    _LOCAL_RAW = text
    _suppress_watch()


def _bootstrap_local_doc(doc: dict) -> None:
    """
    首次创建本地层文件时补齐骨架。
    只在当前 doc 缺少对应字段时才补充,
    不覆盖现有用户设置。这样首次点击
    "新增模型"保存后会生成结构完整的本地文件,
    后续再调用不会重新覆盖。
    """
    if "current_model" not in doc:
        first = next(
            (m.get("id") for m in doc.get("models", []) if isinstance(m, dict) and m.get("id")),
            None,
        )
        doc["current_model"] = first
    if "availableModels" not in doc:
        ids = [m.get("id") for m in doc.get("models", []) if isinstance(m, dict) and m.get("id")]
        doc["availableModels"] = ids


def _commit_local(doc: dict) -> None:
    """
    写本地覆盖层并就地重装配置(锁内调用)。
    重装要重建 OpenAI client,实测能耗掉一秒量级,足以让写入时开的抑制窗口先过期,
    所以末尾再计一次时,保证 250ms 防抖定时器醒来时仍落在窗口内。
    """
    _save_local(doc)
    _reload_locked()
    _suppress_watch()


def _write_local_current(model_id: str) -> tuple[bool, str]:
    """把 current_model 写进本地覆盖层(锁内调用),失败返回 (False, 原因)。"""
    try:
        doc, _ = _read_local(_LOCAL_PATH)
        doc["current_model"] = model_id
        if not isinstance(doc.get("models"), list):
            doc["models"] = []
        _save_local(doc)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def set_current_model(model_id: str) -> bool:
    """
    热切换当前模型。会同步重建 OpenAI client(不同模型可能 base_url/apiKey 不同),
    并把 current_model 持久化进 models/model.local.json,这样 reload 时不会被覆盖回旧值。
    带 key 的 model.json 在这条路径上一个字节都不会被改写;本地文件写不进去
    (只读挂载、无权限等)就退回只更新内存,切换本身依然生效。
    Returns: 切换成功返回 True,id 不存在或在白名单外返回 False。
    """
    with _STATE_LOCK:
        entry = _find_model(_PARSED, model_id)
        if not entry:
            return False
        if _AVAILABLE is not None and model_id not in _AVAILABLE:
            return False
        _install_current(model_id)
        _CONFIG["current_model"] = model_id
        ok, err = _write_local_current(model_id)
        if not ok:
            print(f"[models] current_model 未落盘,仅更新内存: {err}", flush=True)
        return True


def reload_config(config_path: str | os.PathLike | None = None) -> dict:
    """
    重新读取 model.json(叠加同目录的 model.local.json)并更新所有配置,实现热切换。
    Args:
        config_path: 可选,指定新的 model.json 路径;None 则使用模块默认路径
    Returns:
        切换后生效的完整配置 {"current_model": ..., "base_url": ..., "models": [...]}
    """
    path = Path(config_path) if config_path else _CONFIG_PATH
    layers = _load_layers(path)
    with _STATE_LOCK:
        _install_layers(layers)
        return {
            "current_model": _CURRENT_MODEL,
            "base_url": _BASE_URL,
            "models": [dict(m) for m in _MODELS],
        }


def reload_models_list() -> None:
    """
    只刷新模型列表(支持新增/删除),不动当前 current_model / base_url / apiKey。
    适合 /api/models 这种前端定期拉取模型列表的场景,避免覆盖用户刚切换的模型。
    """
    layers = _load_layers(_CONFIG_PATH)
    with _STATE_LOCK:
        global _CONFIG, _PARSED, _AVAILABLE, _MODELS, _LAST_RAW
        global _LOCAL_RAW, _ORIGINS, _KEY_BASES, _TRACKED_IDS
        _CONFIG = layers.cfg
        _PARSED = _parse_config(layers.cfg)
        _AVAILABLE = _PARSED["available"]
        _MODELS = _filtered_models(_PARSED)
        _LAST_RAW = layers.raw
        _LOCAL_RAW = layers.local_raw
        _ORIGINS = layers.origins
        _KEY_BASES = layers.key_bases
        _TRACKED_IDS = layers.tracked_ids


# ════════════════════════════════════════════════════════════
#                  模型管理(只写本地覆盖层)
# ════════════════════════════════════════════════════════════
# 对外四个入口都遵守同一条铁律:只读 model.json,只写 model.local.json,
# 且任何返回值里都不出现 apiKey 明文。

_SPEC_STR_FIELDS = ("name", "vendor", "url", "apiKeyEnv")
_SPEC_INT_FIELDS = ("maxInputTokens", "maxOutputTokens")
_SPEC_BOOL_FIELDS = ("supportsToolCall",)


def _redact(text: str, secret: str) -> str:
    """兜底脱敏:错误信息里万一带上 key,换成占位符再返回。"""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def list_models_admin() -> list[dict]:
    """
    管理视图的模型全表(不受 availableModels 白名单裁剪,便于把被隐藏的条目也管起来)。
    绝不返回 apiKey 明文,只报告 key 有没有(hasKey)和来自哪一层(keySource)。
    editable 表示条目由本地覆盖层提供,只有它能被 delete_model 删掉。
    """
    with _STATE_LOCK:
        entries = [dict(m) for m in _PARSED.get("models", [])]
        origins = dict(_ORIGINS)
        bases = dict(_KEY_BASES)
    out = []
    for entry in entries:
        mid = entry.get("id") or ""
        source = _key_source(entry, bases.get(mid, "none"))
        origin = origins.get(mid, "tracked")
        out.append({
            "id": mid,
            "name": entry.get("name") or mid,
            "vendor": entry.get("vendor") or "",
            "url": entry.get("url") or "",
            "maxInputTokens": entry.get("maxInputTokens"),
            "maxOutputTokens": entry.get("maxOutputTokens"),
            "supportsToolCall": bool(entry.get("supportsToolCall")),
            "hasKey": source != "none",
            "keySource": source,
            "origin": origin,
            "editable": origin == "local",
        })
    return out


def key_source(model_id: str) -> str:
    """返回该模型 key 的来源:'env' | 'local' | 'tracked' | 'none'。未知 id 一律 'none'。"""
    with _STATE_LOCK:
        entry = _find_model(_PARSED, model_id)
        base = _KEY_BASES.get(model_id, "none")
    if entry is None:
        return "none"
    return _key_source(entry, base)


def _apply_spec(entry: dict, spec: dict) -> str:
    """
    把 spec 里出现的白名单字段写进本地条目,返回空串表示成功,否则返回错误原因。
    未列在白名单里的键一律忽略,避免前端脏字段灌进配置。
    apiKey 缺省或空串保持原值,显式 null 才清除本地 key。
    """
    for key in _SPEC_STR_FIELDS:
        if key not in spec:
            continue
        value = spec[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            entry.pop(key, None)
            continue
        if not isinstance(value, str):
            return f"{key} 必须是字符串"
        entry[key] = value.strip()
    for key in _SPEC_INT_FIELDS:
        if key not in spec:
            continue
        value = spec[key]
        if value is None or value == "":
            entry.pop(key, None)
            continue
        parsed = _non_negative_int(value)
        if not parsed:
            return f"{key} 必须是正整数"
        entry[key] = parsed
    for key in _SPEC_BOOL_FIELDS:
        if key not in spec:
            continue
        value = spec[key]
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = bool(value)
    if "apiKey" in spec:
        value = spec["apiKey"]
        if value is None:
            entry.pop("apiKey", None)
        elif not isinstance(value, str):
            return "apiKey 必须是字符串或 null"
        elif value.strip():
            entry["apiKey"] = value.strip()
    return ""


def _local_models(doc: dict) -> list:
    """取本地文档里的 models 数组,结构不对就当空表。"""
    models = doc.get("models")
    return models if isinstance(models, list) else []


def _keep_visible(doc: dict, model_id: str) -> None:
    """
    白名单存在时,把新 id 补进本地的 availableModels,否则新增的模型在下拉框里看不见。
    基线取当前生效的白名单(已含本地覆盖),再追加。
    """
    if _AVAILABLE is None or model_id in _AVAILABLE:
        return
    allowed = doc.get("availableModels")
    if not isinstance(allowed, list):
        allowed = list(_CONFIG.get("availableModels") or [])
    if model_id not in allowed:
        allowed.append(model_id)
    doc["availableModels"] = allowed


def upsert_model(spec: dict) -> dict:
    """
    新增或修改一个模型条目,只写 models/model.local.json。
    spec 可含 id/name/vendor/url/apiKey/apiKeyEnv/maxInputTokens/maxOutputTokens/supportsToolCall;
    id 必填,apiKey 缺省或空串表示不改动已有 key,显式 null 表示清除本地 key。
    Returns: {'ok': True} 或 {'ok': False, 'error': '...'}
    """
    if not isinstance(spec, dict):
        return {"ok": False, "error": "spec 必须是对象"}
    raw_id = spec.get("id")
    model_id = raw_id.strip() if isinstance(raw_id, str) else ""
    if not model_id:
        return {"ok": False, "error": "缺少 id"}
    with _STATE_LOCK:
        try:
            doc, _ = _read_local(_LOCAL_PATH)
            models = _local_models(doc)
            entry = next(
                (m for m in models if isinstance(m, dict) and m.get("id") == model_id),
                None,
            )
            if entry is None:
                entry = {"id": model_id}
                models.append(entry)
            error = _apply_spec(entry, spec)
            if error:
                return {"ok": False, "error": error}
            if not entry.get("url"):
                inherited = _find_model(_PARSED, model_id)
                if not (inherited and inherited.get("url")):
                    return {"ok": False, "error": "缺少 url"}
            doc["models"] = models
            _keep_visible(doc, model_id)
            _bootstrap_local_doc(doc)
            _commit_local(doc)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True}


def delete_model(model_id: str) -> dict:
    """
    删除本地覆盖层里的条目。model.json 里的条目不可删(origin=='tracked'),
    删掉对跟踪条目的覆盖后,该 id 回落到 model.json 的原始定义。
    Returns: {'ok': True} 或 {'ok': False, 'error': '...'}
    """
    mid = model_id.strip() if isinstance(model_id, str) else ""
    if not mid:
        return {"ok": False, "error": "缺少 id"}
    with _STATE_LOCK:
        try:
            doc, _ = _read_local(_LOCAL_PATH)
            models = _local_models(doc)
            kept = [m for m in models if not (isinstance(m, dict) and m.get("id") == mid)]
            if len(kept) == len(models):
                return {"ok": False, "error": f"不可删除: {mid} 不在本地配置里"}
            doc["models"] = kept
            if mid not in _TRACKED_IDS:
                if doc.get("current_model") == mid:
                    doc.pop("current_model", None)
                allowed = doc.get("availableModels")
                if isinstance(allowed, list) and mid in allowed:
                    doc["availableModels"] = [x for x in allowed if x != mid]
            _commit_local(doc)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True}


def test_model(model_id: str) -> dict:
    """
    用三级解析出的 key 发一个最小请求,验证这条配置是否真的能用。
    Returns: {'ok': True, 'latency_ms': 123} 或 {'ok': False, 'error': '...'}
    错误信息做过脱敏,不会把 key 带出去。
    """
    mid = model_id.strip() if isinstance(model_id, str) else ""
    with _STATE_LOCK:
        entry = _find_model(_PARSED, mid)
        if entry is None:
            return {"ok": False, "error": f"未知模型: {mid or model_id}"}
        cfg = _CONFIG
        api_key = _pick_api_key(cfg, entry)
        base_url = _pick_base_url(cfg, entry)
        timeout = min(_resolve_timeout(entry, cfg), _TEST_TIMEOUT)
    if not api_key:
        return {"ok": False, "error": "没有可用的 key(环境变量 / 本地层 / 跟踪层都是空的)"}
    started = time.monotonic()
    try:
        client = _build_client(api_key, base_url, timeout)
        client.chat.completions.create(
            model=mid,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            timeout=timeout,
        )
    except Exception as e:
        return {"ok": False, "error": _redact(f"{type(e).__name__}: {e}", api_key)}
    return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000)}


# ════════════════════════════════════════════════════════════
#                      超时 + 有限退避重试
# ════════════════════════════════════════════════════════════

def _status_of(exc: Exception) -> int | None:
    """尽力取出 HTTP 状态码,取不到返回 None。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _is_retryable(exc: Exception) -> bool:
    """连接错误 / 超时 / 429 / 5xx 视为可重试,其余(如 400/401)直接抛。"""
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if not isinstance(exc, APIStatusError):
        return False
    status = _status_of(exc)
    if status is None:
        return False
    return status == 429 or status >= 500


def _retry_after(exc: Exception) -> float | None:
    """尽力从响应头读 Retry-After(秒),缺失或超出上限返回 None。"""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        v = float(headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None
    if v <= 0 or v > _RETRY_AFTER_CAP:
        return None
    return v


def _backoff_delay(attempt: int, exc: Exception) -> float:
    """指数退避 + 抖动;服务端给了 Retry-After 就听它的。"""
    hinted = _retry_after(exc)
    if hinted is not None:
        return hinted
    delay = min(_RETRY_BASE_DELAY * (2 ** attempt), _RETRY_MAX_DELAY)
    return delay + random.uniform(0, delay * 0.25)


def _log_retry(label: str, attempt: int, delay: float, exc: Exception) -> None:
    """重试前打一行日志,方便定位挂住的 provider。"""
    print(
        f"[models] {label} 第 {attempt + 1} 次重试(等待 {delay:.1f}s): "
        f"{type(exc).__name__}: {exc}",
        flush=True,
    )


def _call_with_retry(make_call, max_retries: int, label: str):
    """执行一次模型调用,对可重试错误做有限次指数退避重试。"""
    attempt = 0
    while True:
        try:
            return make_call()
        except Exception as e:
            if attempt >= max_retries or not _is_retryable(e):
                raise
            delay = _backoff_delay(attempt, e)
            _log_retry(label, attempt, delay, e)
            time.sleep(delay)
            attempt += 1


def _resolve_attempts(snap: ModelSnapshot, retries: int | None) -> int:
    """调用方显式传 retries 就用它,否则用快照里的配置值。"""
    if retries is None:
        return snap.max_retries
    value = _non_negative_int(retries)
    return snap.max_retries if value is None else value


def _build_kwargs(
    snap: ModelSnapshot,
    messages: list,
    tools: list | None,
    use_thinking: bool,
    stream: bool,
    timeout: float | None,
) -> dict:
    """组装 chat.completions.create 的参数。"""
    kwargs = dict(
        model=snap.model_id,
        messages=messages,
        stream=stream,
        timeout=_positive_float(timeout) or snap.timeout,
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if use_thinking and snap.supports_reasoning:
        kwargs["extra_body"] = {"reasoning_split": True}
    return kwargs


# ════════════════════════════════════════════════════════════
#                          同步请求
# ════════════════════════════════════════════════════════════

def request(
    message: str | None = None,
    messages: list | None = None,
    tools: list | None = None,
    use_thinking: bool = True,
    timeout: float | None = None,
    retries: int | None = None,
):
    """
    调用模型,支持工具调用。工具列表默认从 agent.tool.get_tools() 复用。
    超时默认 120 秒(可用 model.json 的 timeout 字段或 CODEFORGE_MODEL_TIMEOUT 覆盖),
    429 / 5xx / 连接错误按指数退避重试有限次,挂住的连接不会永久占住工作线程。
    返回 message 对象(不是字符串),无工具时用 .content 拿正文,有工具时检查 .tool_calls。
    """
    if messages is None:
        if message is None:
            raise ValueError("必须传 message 或 messages")
        messages = [
            {'role': 'system', 'content': 'You named CodeForge, an AI Agent IDE on Android Termux.'},
            {"role": "user", "content": message},
        ]

    if tools is None:
        from agent.tool import get_tools
        tools = get_tools() or None

    snap = _snapshot()
    kwargs = _build_kwargs(snap, messages, tools, use_thinking, False, timeout)
    resp = _call_with_retry(
        lambda: snap.client.chat.completions.create(**kwargs),
        _resolve_attempts(snap, retries),
        "request",
    )
    return resp.choices[0].message


# ════════════════════════════════════════════════════════════
#              流式输出(MiniMax / OpenAI 兼容)
# ════════════════════════════════════════════════════════════

class _StreamMsg:
    """把流式 chunk 累积还原成 message 形状,供 loop 后续判断 tool_calls。"""
    def __init__(self, content, reasoning, tool_calls):
        self.role = "assistant"
        self.content = content
        self.reasoning_content = reasoning
        self.tool_calls = tool_calls


class _TCFunction:
    """还原后的 tool_call.function。"""
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _TC:
    """还原后的 tool_call,字段与 OpenAI SDK 的对象保持一致。"""
    def __init__(self, slot: dict):
        self.id = slot["id"]
        self.type = slot.get("type") or "function"
        self.index = slot.get("index", 0)
        self.function = _TCFunction(slot["function"]["name"], slot["function"]["arguments"])


def _attr(obj, name: str, default=None):
    """兼容对象属性与 dict 两种 chunk 形态地取字段。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class _ToolCallAccumulator:
    """
    流式 tool_calls 增量重组器。
    provider 给了 index 就按 index 归槽;省略 index 时按出现顺序回退分配槽位:
      - 带新 id  → 开新槽位(若上一个槽位还没拿到 id,则认为是同一次调用的补发)
      - 无 id    → 并入上一个槽位(arguments 续传)
      - 无 id 但带了与上一个槽位不同的 function.name → 判定为新的一次调用
    这样 id 后到、name 后到、arguments 跨 chunk 拼接都不会丢增量。
    """
    def __init__(self):
        self._slots: dict[int, dict] = {}
        self._by_id: dict[str, int] = {}
        self._last: int | None = None
        self._next: int = 0

    def _new_slot(self) -> int:
        """分配一个未被占用的槽位序号。"""
        while self._next in self._slots:
            self._next += 1
        idx = self._next
        self._next += 1
        return idx

    def _ensure(self, idx: int) -> dict:
        """取槽位,不存在则建。"""
        slot = self._slots.get(idx)
        if slot is None:
            slot = {
                "id": "", "type": "function", "index": idx,
                "function": {"name": "", "arguments": ""},
            }
            self._slots[idx] = slot
        return slot

    def _slot_index(self, tc) -> int:
        """决定这一片增量该落到哪个槽位。"""
        idx = _non_negative_int(_attr(tc, "index"))
        tc_id = _attr(tc, "id") or ""
        if idx is not None:
            if tc_id:
                self._by_id[tc_id] = idx
            return idx
        if tc_id:
            known = self._by_id.get(tc_id)
            if known is not None:
                return known
            if self._last is not None and not self._slots[self._last]["id"]:
                self._by_id[tc_id] = self._last
                return self._last
            fresh = self._new_slot()
            self._by_id[tc_id] = fresh
            return fresh
        if self._last is not None:
            name = _attr(_attr(tc, "function"), "name")
            current = self._slots[self._last]["function"]["name"]
            if name and current and name != current:
                return self._new_slot()
            return self._last
        return self._new_slot()

    def feed(self, tool_calls) -> None:
        """吃掉一个 delta 里的 tool_calls 增量。"""
        for tc in tool_calls or []:
            idx = self._slot_index(tc)
            slot = self._ensure(idx)
            tc_id = _attr(tc, "id")
            if tc_id:
                slot["id"] = tc_id
            tc_type = _attr(tc, "type")
            if tc_type:
                slot["type"] = tc_type
            fn = _attr(tc, "function")
            if fn is not None:
                name = _attr(fn, "name")
                if name:
                    slot["function"]["name"] = name
                args = _attr(fn, "arguments")
                if args:
                    slot["function"]["arguments"] += args
            self._last = idx

    def build(self) -> list | None:
        """按槽位序号还原成与 request() 兼容的 tool_calls 列表,没有则 None。"""
        if not self._slots:
            return None
        return [_TC(self._slots[i]) for i in sorted(self._slots)]


def _iter_stream_events(chunks):
    """
    把 chunk 序列重组成事件流。纯逻辑、不碰网络,可直接喂假 chunk 做单测。
    逐个 yield:
      {'type': 'reasoning', 'text': '...'}
      {'type': 'content',   'text': '...'}
      {'type': 'done',      'message': <_StreamMsg>}
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    acc = _ToolCallAccumulator()

    for chunk in chunks:
        choices = _attr(chunk, "choices")
        if not choices:
            continue
        delta = _attr(choices[0], "delta")
        if delta is None:
            continue

        r = _attr(delta, "reasoning_content")
        if r:
            reasoning_parts.append(r)
            yield {"type": "reasoning", "text": r}

        c = _attr(delta, "content")
        if c:
            content_parts.append(c)
            yield {"type": "content", "text": c}

        acc.feed(_attr(delta, "tool_calls"))

    yield {
        "type": "done",
        "message": _StreamMsg(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=acc.build(),
        ),
    }


def request_stream(
    messages: list,
    tools: list | None = None,
    use_thinking: bool = True,
    timeout: float | None = None,
    retries: int | None = None,
):
    """
    流式生成器,逐步 yield 事件:
      {'type': 'reasoning', 'text': '...'}
      {'type': 'content',   'text': '...'}
      {'type': 'done',      'message': <_StreamMsg>}
    与 request() 输出语义一致,只是边收边推。
    超时与重试策略同 request();但只要已经吐出过增量就不再重试,避免重复内容。
    """
    snap = _snapshot()
    kwargs = _build_kwargs(snap, messages, tools, use_thinking, True, timeout)
    max_retries = _resolve_attempts(snap, retries)

    attempt = 0
    while True:
        emitted = False
        try:
            stream = snap.client.chat.completions.create(**kwargs)
            for event in _iter_stream_events(stream):
                emitted = True
                yield event
            return
        except Exception as e:
            if emitted or attempt >= max_retries or not _is_retryable(e):
                raise
            delay = _backoff_delay(attempt, e)
            _log_retry("request_stream", attempt, delay, e)
            time.sleep(delay)
            attempt += 1


# ============================================================================
#  热更新:监听 model.json 与 model.local.json,自动 reload_config()
# ----------------------------------------------------------------------------
#  设计要点:
#   1. watchdog 不可用 → 静默退化,只支持 /api/models 手动 reload
#   2. 防抖 250ms:VSCode/Vim 一次保存会触发多个事件(modify + modify + ...)
#   3. 监听父目录再过滤文件名:有些编辑器是"写到 tmp + rename",
#      直接监听文件路径在 rename 那一刻会丢失;本地层文件一开始可能还不存在,
#      监听目录同样能接住它被创建的那一刻
#   4. 本进程自己写本地层(set_current_model / upsert / delete)时抑制 1s,
#      再叠加"两层内容都没变则跳过",双保险不会自触发 reload 风暴
#   5. 后台 daemon 线程,进程退出时随主线程一起死
# ============================================================================
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    _HAS_WATCHDOG = True
except Exception:
    _HAS_WATCHDOG = False

_watcher_started = False
_watcher_lock = threading.Lock()


class _ModelFileHandler(FileSystemEventHandler if _HAS_WATCHDOG else object):
    """两层配置文件任一改动后,debounce 再 reload。"""
    def __init__(self, targets):
        self._targets = {Path(t).resolve() for t in targets}
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _is_target(self, event: "FileSystemEvent") -> bool:
        try:
            p = Path(getattr(event, "dest_path", None) or event.src_path).resolve()
        except Exception:
            return False
        return p in self._targets

    def _trigger(self):
        if _watch_suppressed():
            return
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
            if not event.is_directory and self._is_target(event):
                self._trigger()


def _safe_reload():
    """线程安全的 reload:抑制窗口内或两层内容都没变就跳过,出错只打日志不抛。"""
    try:
        if _watch_suppressed():
            return
        raw = _CONFIG_PATH.read_text(encoding="utf-8")
        _, local_raw = _read_local(_LOCAL_PATH)
        with _STATE_LOCK:
            if raw == _LAST_RAW and local_raw == _LOCAL_RAW:
                return
        cfg = reload_config()
        print(f"[models] hot-reloaded: current={cfg.get('current_model')!r} "
              f"models={len(cfg.get('models', []))}", flush=True)
    except Exception as e:
        print(f"[models] hot-reload failed: {type(e).__name__}: {e}", flush=True)


def start_watcher(daemon: bool = True) -> bool:
    """
    启动配置文件监听(只启动一次,重复调用安全)。
    同时盯 model.json 与 model.local.json,后者此刻不存在也没关系,监听的是目录。
    Returns: True 表示监听已就绪,False 表示退化(无 watchdog 或启动失败)。
    """
    global _watcher_started
    with _watcher_lock:
        if _watcher_started:
            return True
        if not _HAS_WATCHDOG:
            return False
        try:
            targets = [_CONFIG_PATH, _LOCAL_PATH]
            handler = _ModelFileHandler(targets)
            obs = Observer()
            for watch_dir in {p.resolve().parent for p in targets}:
                obs.schedule(handler, str(watch_dir), recursive=False)
            obs.daemon = daemon
            obs.start()
            _watcher_started = True
            print(f"[models] watching {_CONFIG_PATH.name} + {_LOCAL_PATH.name} "
                  f"(debounce {_WATCH_DEBOUNCE}s)", flush=True)
            return True
        except Exception as e:
            print(f"[models] watcher start failed: {type(e).__name__}: {e}", flush=True)
            return False


start_watcher()


# ════════════════════════════════════════════════════════════
#              流式重组自检(不依赖网络,python -m models.request)
# ════════════════════════════════════════════════════════════

def _fake_chunk(content=None, reasoning=None, tool_calls=None) -> dict:
    """构造一个最小 chunk,字段形状与 OpenAI 流式返回一致。"""
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"choices": [{"delta": delta}]}


def _fake_tc(index=None, tc_id=None, name=None, arguments=None) -> dict:
    """构造一片 tool_calls 增量,None 的字段表示 provider 没发。"""
    tc: dict = {}
    if index is not None:
        tc["index"] = index
    if tc_id is not None:
        tc["id"] = tc_id
    fn: dict = {}
    if name is not None:
        fn["name"] = name
    if arguments is not None:
        fn["arguments"] = arguments
    if fn:
        tc["function"] = fn
    return tc


def _collect(chunks) -> tuple[list, object]:
    """跑一遍重组逻辑,返回 (增量事件列表, 最终 message)。"""
    events = list(_iter_stream_events(chunks))
    return events[:-1], events[-1]["message"]


def _selftest() -> int:
    """流式重组的最小单测。返回失败用例数。"""
    cases = []

    def case(name):
        def deco(fn):
            cases.append((name, fn))
            return fn
        return deco

    @case("正文与思考分片按序拼接")
    def _t1():
        deltas, msg = _collect([
            _fake_chunk(reasoning="想"),
            _fake_chunk(reasoning="一下"),
            _fake_chunk(content="你"),
            _fake_chunk(content="好"),
        ])
        assert [d["type"] for d in deltas] == ["reasoning", "reasoning", "content", "content"]
        assert msg.reasoning_content == "想一下"
        assert msg.content == "你好"
        assert msg.tool_calls is None

    @case("arguments 跨 chunk 拼接(带 index)")
    def _t2():
        _, msg = _collect([
            _fake_chunk(tool_calls=[_fake_tc(index=0, tc_id="c1", name="read_file", arguments='{"pa')]),
            _fake_chunk(tool_calls=[_fake_tc(index=0, arguments='th": "/a')]),
            _fake_chunk(tool_calls=[_fake_tc(index=0, arguments='.py"}')]),
        ])
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].id == "c1"
        assert msg.tool_calls[0].function.name == "read_file"
        assert msg.tool_calls[0].function.arguments == '{"path": "/a.py"}'

    @case("index 缺失时按出现顺序回退分槽")
    def _t3():
        _, msg = _collect([
            _fake_chunk(tool_calls=[_fake_tc(tc_id="c1", name="list_dir", arguments='{"pa')]),
            _fake_chunk(tool_calls=[_fake_tc(arguments='th": "/"}')]),
            _fake_chunk(tool_calls=[_fake_tc(tc_id="c2", name="read_file", arguments='{"path"')]),
            _fake_chunk(tool_calls=[_fake_tc(arguments=': "/b.py"}')]),
        ])
        assert [tc.id for tc in msg.tool_calls] == ["c1", "c2"]
        assert [tc.function.name for tc in msg.tool_calls] == ["list_dir", "read_file"]
        assert msg.tool_calls[0].function.arguments == '{"path": "/"}'
        assert msg.tool_calls[1].function.arguments == '{"path": "/b.py"}'

    @case("index 缺失且 id 后到")
    def _t4():
        _, msg = _collect([
            _fake_chunk(tool_calls=[_fake_tc(name="read_file", arguments='{"path"')]),
            _fake_chunk(tool_calls=[_fake_tc(tc_id="late-1", arguments=': "/c.py"}')]),
        ])
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].id == "late-1"
        assert msg.tool_calls[0].function.arguments == '{"path": "/c.py"}'

    @case("name 后到")
    def _t5():
        _, msg = _collect([
            _fake_chunk(tool_calls=[_fake_tc(index=0, tc_id="c9", arguments='{"path"')]),
            _fake_chunk(tool_calls=[_fake_tc(index=0, name="read_file", arguments=': "/d.py"}')]),
        ])
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].function.name == "read_file"
        assert msg.tool_calls[0].function.arguments == '{"path": "/d.py"}'

    @case("无 index 无 id 靠 name 变化切槽")
    def _t6():
        _, msg = _collect([
            _fake_chunk(tool_calls=[_fake_tc(name="list_dir", arguments="{}")]),
            _fake_chunk(tool_calls=[_fake_tc(name="read_file", arguments='{"path": "/e.py"}')]),
        ])
        assert [tc.function.name for tc in msg.tool_calls] == ["list_dir", "read_file"]

    @case("并行 tool_calls 与空 chunk")
    def _t7():
        _, msg = _collect([
            {"choices": []},
            _fake_chunk(tool_calls=[
                _fake_tc(index=0, tc_id="a", name="list_dir", arguments="{}"),
                _fake_tc(index=1, tc_id="b", name="read_file", arguments='{"path"'),
            ]),
            _fake_chunk(tool_calls=[_fake_tc(index=1, arguments=': "/f.py"}')]),
        ])
        assert [tc.id for tc in msg.tool_calls] == ["a", "b"]
        assert msg.tool_calls[1].function.arguments == '{"path": "/f.py"}'

    failed = 0
    for name, fn in cases:
        try:
            fn()
            print(f"  PASS  {name}", flush=True)
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e}", flush=True)
    print(f"[selftest] {len(cases) - failed}/{len(cases)} passed", flush=True)
    return failed


if __name__ == "__main__":
    raise SystemExit(_selftest())
