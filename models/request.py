import json
import os
from pathlib import Path

from openai import OpenAI

# ============ 启动时只读一次的配置 ============
# 模块级代码在 import 时执行一次,Python 的模块缓存保证不会重复加载,
# 因此 model.json 在整个进程生命周期内默认只被读取一次;
# 如需热切换,调用 reload_config() / set_current_model() 即可。
_CONFIG_PATH = Path(__file__).resolve().parent / "model.json"
_CONFIG: dict = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
_CURRENT_MODEL: str = _CONFIG["current_model"]
_BASE_URL: str = _CONFIG["base_url"]
_MODELS: list[dict] = list(_CONFIG.get("models", []))


# ============ 启动时只创建一次的客户端 ============
_client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY") or "sk-a32dc24893fc478782451979668eb124",
    base_url=_BASE_URL,
)


# ============ 公开 API ============

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
    # 注意:_client 已用旧 base_url 创建;若切换了 endpoint,需重启进程或手动重建 _client
    return {
        "current_model": _CURRENT_MODEL,
        "base_url": _BASE_URL,
        "models": _MODELS,
    }


def request(message: str) -> str:
    """调用 model.json 中 current_model 指定的模型,完成一次对话。"""
    response = _client.chat.completions.create(
        model=_CURRENT_MODEL,
        messages=[{"role": "user", "content": message}],
        stream=False,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
    )
    return response.choices[0].message.content
