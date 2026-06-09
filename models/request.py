import json
import os
from pathlib import Path
from openai import OpenAI

_CONFIG_PATH = Path(__file__).resolve().parent / "model.json"
_CONFIG: dict = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
_CURRENT_MODEL: str = _CONFIG["current_model"]
_BASE_URL: str = _CONFIG["base_url"]
_MODELS: list[dict] = list(_CONFIG.get("models", []))


_client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY") or "sk-a32dc24893fc478782451979668eb124",
    base_url=_BASE_URL,
)
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
    热切换当前模型。仅在内存中改 _CURRENT_MODEL,不动文件。
    Returns: 切换成功返回 True,id 不存在返回 False。
    """
    global _CURRENT_MODEL
    if not any(m.get("id") == model_id for m in _MODELS):
        return False
    _CURRENT_MODEL = model_id
    return True


def reload_config(config_path: str | os.PathLike | None = None) -> dict:
    """
    重新读取 model.json 并更新所有配置,实现热切换。
    Args:
        config_path: 可选,指定新的 model.json 路径;None 则使用模块默认路径
    Returns:
        切换后生效的完整配置 {"current_model": ..., "base_url": ..., "models": [...]}
    """
    global _CONFIG, _CURRENT_MODEL, _BASE_URL, _MODELS
    path = Path(config_path) if config_path else _CONFIG_PATH
    _CONFIG = json.loads(path.read_text(encoding="utf-8"))
    _CURRENT_MODEL = _CONFIG["current_model"]
    _BASE_URL = _CONFIG["base_url"]
    _MODELS = list(_CONFIG.get("models", []))
    return {
        "current_model": _CURRENT_MODEL,
        "base_url": _BASE_URL,
        "models": _MODELS,
    }


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
    if use_thinking:
        kwargs["reasoning_effort"] = "high"
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

    return _client.chat.completions.create(**kwargs).choices[0].message
