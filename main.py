"""
CodeForge 的 HTTP 层。

所有接口都在 auth 的三道关卡之后:Host 白名单 -> Origin 同源 -> X-CodeForge-Token,
只有 GET / 和 /static/* 豁免 token。会话按 X-CodeForge-Session 隔离,缺省落到
default 会话;history / plan_model / auto / pending / diff 全部挂在会话上,不再有
进程级全局状态。

路径一律先过 sandbox.resolve(),越界统一由 errorhandler 收敛成 403 JSON;
文件读写走 explorer 的编码探测与原子写,不再用 utf-8 + errors='replace' 覆写。
"""
import codecs
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request as flask_request, Response
from werkzeug.exceptions import HTTPException

import auth
import sandbox
from explorer import file as explorer_file
from explorer.file import MAX_TEXT_BYTES, list_dir, create as file_create
from explorer.search import search as file_search
from models.request import (
    get_models,
    get_current_model,
    get_base_url,
    set_current_model,
    list_models_admin,
    upsert_model,
    delete_model,
    test_model,
)
from agent import session as agent_session
from agent import loop as agent_loop
from git import engine as agent_git

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024

DEFAULT_PORT = 9191


# ════════════════════════════════════════════════════════════
#                      应用配置(.config.json)
# ════════════════════════════════════════════════════════════

_CONFIG_PATH = Path(__file__).resolve().parent / ".config.json"
_CFG_TTL   = 2.0
_CFG_LOCK  = threading.RLock()
_CFG_CACHE = {"data": {}, "at": 0.0, "sig": None}


def _to_bool(v, default: bool = False) -> bool:
    """容错地把 'true'/'false' 字符串 / bool 转成 bool。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ('true', '1', 'yes', 'on')
    if v is None:
        return default
    return bool(v)


def _config_sig():
    """配置文件的 (mtime, size) 指纹,取不到返回 None。"""
    try:
        st = _CONFIG_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def app_config(force: bool = False) -> dict:
    """
    读 .config.json。带 2 秒缓存,缓存期外按 (mtime, size) 判断是否真的要重读,
    所以改完配置文件不需要重启进程。读失败或不是对象时退回空 dict。
    """
    now = time.monotonic()
    with _CFG_LOCK:
        fresh = _CFG_CACHE["at"] > 0 and (now - _CFG_CACHE["at"]) < _CFG_TTL
        if not force and fresh:
            return _CFG_CACHE["data"]
        sig = _config_sig()
        if not force and _CFG_CACHE["at"] > 0 and sig == _CFG_CACHE["sig"]:
            _CFG_CACHE["at"] = now
            return _CFG_CACHE["data"]
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        _CFG_CACHE["data"] = data
        _CFG_CACHE["at"]   = now
        _CFG_CACHE["sig"]  = sig
        return data


def get_flow() -> bool:
    """是否默认走流式输出。"""
    return _to_bool(app_config().get('flow'), False)


def get_max_round() -> int:
    """Agent 单次任务的轮数上限,非法值退回 20。"""
    try:
        n = int(app_config().get('max_round', 20))
    except (TypeError, ValueError):
        return 20
    return n if 1 <= n <= 200 else 20


def _write_config(patch: dict) -> dict:
    """把 patch 合并进 .config.json 并原子写回,返回写入后的完整配置。"""
    with _CFG_LOCK:
        data = dict(app_config(force=True))
        data.update(patch)
        explorer_file.write_text(
            str(_CONFIG_PATH),
            json.dumps(data, ensure_ascii=False, indent=4) + "\n",
        )
        _CFG_CACHE["at"] = 0.0
        return app_config(force=True)


# ════════════════════════════════════════════════════════════
#                      认证与错误收敛
# ════════════════════════════════════════════════════════════

_env_port = (os.environ.get('CODEFORGE_PORT') or '').strip()
if _env_port.isdigit():
    auth.configure(port=int(_env_port))
else:
    auth.configure()

auth.install(app)


@app.errorhandler(sandbox.SandboxError)
def on_sandbox_error(e):
    """路径越界/写受保护文件统一成 403 JSON,不再冒泡成 HTML 500。"""
    return jsonify({"ok": False, "error": str(e), "code": "sandbox"}), 403


@app.errorhandler(HTTPException)
def on_http_error(e):
    """/api/* 下的 HTTP 错误(413/404/405 等)返回 JSON,页面路由保持原样。"""
    if not flask_request.path.startswith('/api/'):
        return e.get_response()
    return jsonify({
        "ok": False,
        "error": e.description,
        "code": (e.name or '').lower().replace(' ', '_'),
    }), e.code


@app.errorhandler(Exception)
def on_unhandled_error(e):
    """兜底:任何漏网异常在 /api/* 下也返回 JSON,便于前端提示。"""
    if not flask_request.path.startswith('/api/'):
        raise e
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}", "code": "internal"}), 500


@app.after_request
def no_cache(response):
    """开发期间防止浏览器缓存 HTML/CSS/JS,改完即生效"""
    if response.content_type and any(
        t in response.content_type
        for t in ('text/html', 'text/css', 'application/javascript')
    ):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


# ════════════════════════════════════════════════════════════
#                      会话与取消标志
# ════════════════════════════════════════════════════════════

_CANCEL_LOCK = threading.RLock()
_CANCELLED: set[str] = set()


def _session():
    """取当前请求对应的会话,请求头缺失时落到 default 会话。"""
    return agent_session.get(auth.session_id(flask_request))


def _mark_cancel(sid: str) -> None:
    """置上取消标志,正在跑的 loop 会在下一个事件边界停下。"""
    with _CANCEL_LOCK:
        _CANCELLED.add(sid)


def _cancel_requested(sid: str) -> bool:
    """当前会话是否被请求取消。"""
    with _CANCEL_LOCK:
        return sid in _CANCELLED


def _clear_cancel(sid: str) -> None:
    """清掉取消标志,避免上一次的停止影响下一次运行。"""
    with _CANCEL_LOCK:
        _CANCELLED.discard(sid)


def _pick_history(sess, data: dict) -> list:
    """body 里带 history 就以前端为准,否则用会话里存的那份。"""
    if isinstance(data.get("history"), list):
        return data["history"]
    with sess.lock:
        return list(sess.history)


def _save_history(sess, history: list) -> None:
    """把 loop 跑完的 history 存回会话。"""
    with sess.lock:
        sess.history = history


def _cancelled_result(sess, history: list, rounds: int, tools_used: list) -> dict:
    """构造一次被用户停止的运行结果。"""
    return {
        "answer":     "已停止",
        "history":    history,
        "ok":         False,
        "stopped":    "cancelled",
        "rounds":     rounds,
        "tools_used": tools_used,
        "pending":    sess.pending,
        "sid":        sess.id,
    }


def _run_agent(sess, *, user_message, history, max_rounds, plan_model, cwd) -> dict:
    """
    非流式地跑一次 loop 并返回 done 事件。
    自己消费 run_stream 而不是调 loop.run,是为了能在事件边界响应取消标志:
    生成器只在被拉取时才继续,停止后不会再发起下一轮模型调用或工具调用。
    """
    _clear_cancel(sess.id)
    final = None
    rounds = 0
    tools_used: list[str] = []
    stream = agent_loop.run_stream(
        user_message=user_message,
        history=history,
        max_rounds=max_rounds,
        plan_model=plan_model,
        cwd=cwd,
        sid=sess.id,
    )
    try:
        for ev in stream:
            kind = ev.get("event")
            if kind == "round":
                rounds = ev.get("round", rounds)
            elif kind == "tool_call":
                name = ev.get("name")
                if name and name not in tools_used:
                    tools_used.append(name)
            elif kind == "done":
                final = ev
                break
            if _cancel_requested(sess.id):
                break
    finally:
        stream.close()
        _clear_cancel(sess.id)
    if final is None:
        return _cancelled_result(sess, history, rounds, tools_used)
    final.pop("event", None)
    return final


# ════════════════════════════════════════════════════════════
#                        路径与文件助手
# ════════════════════════════════════════════════════════════

_MOBILE_ROOT = '/sdcard'


def _default_root() -> tuple[str, str]:
    """
    返回 (默认起始目录, 判断依据)。
    依据只看服务端文件系统上 /sdcard 是否真的存在 —— 原来按客户端 User-Agent 判断,
    手机浏览器访问桌面实例时会拿到一个桌面上不存在的 /sdcard。
    """
    try:
        if os.path.isdir(_MOBILE_ROOT):
            return _MOBILE_ROOT, 'sdcard'
    except OSError:
        pass
    return os.path.expanduser('~'), 'home'


def _norm_lf(text: str) -> str:
    """把任意行尾统一成 LF。"""
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _valid_encoding(name: str) -> bool:
    """编码名是否被 Python 认识。"""
    try:
        codecs.lookup(name)
        return True
    except LookupError:
        return False


def _need(data: dict, *names) -> tuple[dict, str]:
    """取一组必填的字符串字段,返回 (值字典, 缺失字段名)。"""
    out = {}
    for n in names:
        v = (data.get(n) or '').strip()
        if not v:
            return out, n
        out[n] = v
    return out, ''


# ============ 页面 ============
@app.route('/')
def index():
    """单页应用入口,唯一不需要 token 的页面。"""
    return app.send_static_file('index.html')


# ============ API ============
@app.get('/api/models')
def api_models():
    """
    返回模型列表 + 当前激活的模型。
    只刷新模型列表(支持新增/删除),不动 current_model,这样用户的热切换选择不会被覆盖。
    """
    from models.request import reload_models_list
    reload_models_list()
    return jsonify({
        "ok":      True,
        "current": get_current_model(),
        "models":  get_models(),
        "base_url": get_base_url(),
    })


@app.post('/api/model')
def api_switch_model():
    """
    切换当前模型。
    Body: {"model": "<model_id>"}
    Returns: {"ok": true, "current": "..."}  /  {"ok": false, "error": "..."}
    """
    data = flask_request.get_json(silent=True) or {}
    model_id = (data.get("model") or "").strip()
    if not model_id:
        return jsonify({"ok": False, "error": "缺少 model 字段"}), 400
    if set_current_model(model_id):
        return jsonify({"ok": True, "current": get_current_model()})
    return jsonify({"ok": False, "error": f"未知模型: {model_id}"}), 404


# ════════════════════════════════════════════════════════════
#                      模型管理(写本地覆盖层)
# ════════════════════════════════════════════════════════════
# 这一组端点管的是 models/model.local.json,model.json 只读不写。
# 三条硬约束:
#   1. 响应出站前必须过 _safe_json,命中密钥就换成 500,而不是把 key 送出去
#   2. 入参先过 _model_spec,非法字段一律 400,不让脏数据落进配置文件
#   3. 测活走守护线程 + 15 秒期限,坏 URL 不会把 Flask 工作线程钉死

_MODEL_TEST_TIMEOUT = 15.0

_SECRET_MIN_LEN = 8
_SECRET_WINDOW = 12

_ID_MAX_LEN = 128
_NAME_MAX_LEN = 128
_URL_MAX_LEN = 512
_ENV_MAX_LEN = 128
_KEY_MAX_LEN = 4096
_MAX_INPUT_TOKENS = 20_000_000
_MAX_OUTPUT_TOKENS = 1_000_000

_ID_BAD_CHARS = ('/', '\\', os.sep, os.altsep or '/')
_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _secret_pool() -> list[str]:
    """
    收集本进程当前解析出的全部模型 key,只供 _leaks_secret 做子串比对。
    返回值绝不进日志、绝不进响应;内部结构取不到时退化成空表(检查降级但不报错)。
    """
    try:
        from models import request as model_request
        pool = []
        with model_request._STATE_LOCK:
            cfg = model_request._CONFIG
            for entry in list(model_request._PARSED.get("models", [])) + [None]:
                value = model_request._pick_api_key(cfg, entry)
                if isinstance(value, str) and len(value) >= _SECRET_MIN_LEN:
                    pool.append(value)
        return pool
    except Exception:
        return []


def _leaks_secret(payload: dict) -> bool:
    """
    出站检查:序列化后的响应里有没有任何模型 key 的明文或长片段。
    整串命中、或任意 12 字符滑窗命中都算泄露;顺带挡住 apiKey / api_key 这两个字段名。
    """
    try:
        blob = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return True
    lowered = blob.lower()
    if '"apikey"' in lowered or '"api_key"' in lowered:
        return True
    for secret in _secret_pool():
        if secret in blob:
            return True
        for i in range(len(secret) - _SECRET_WINDOW + 1):
            if secret[i:i + _SECRET_WINDOW] in blob:
                return True
    return False


def _safe_json(payload: dict, status: int = 200):
    """
    模型管理专用的响应闸门。命中出站密钥检查就一律换成 500,
    宁可让前端拿到错误,也不把 key 发出去。
    """
    if _leaks_secret(payload):
        app.logger.error("模型管理响应命中出站密钥检查,已拦截")
        return jsonify({
            "ok": False,
            "error": "响应命中密钥泄露检查,已拦截",
            "code": "key_leak",
        }), 500
    return jsonify(payload), status


def _bad_request(message: str):
    """模型管理的 400 信封,沿用 {ok, error, code} 三件套。"""
    return jsonify({"ok": False, "error": message, "code": "invalid_spec"}), 400


def _check_str(data: dict, name: str, limit: int) -> tuple[bool, str]:
    """
    校验一个可选字符串字段,返回 (是否通过, 错误说明)。
    字段缺失、为 None、为空串都放行(语义由 models.request 决定:清除或保持原值)。
    """
    if name not in data:
        return True, ''
    value = data[name]
    if value is None:
        return True, ''
    if not isinstance(value, str):
        return False, f"{name} 必须是字符串"
    if len(value) > limit:
        return False, f"{name} 超长(上限 {limit} 字符)"
    return True, ''


def _check_int(data: dict, name: str, limit: int) -> tuple[bool, str]:
    """校验一个可选正整数字段的范围,返回 (是否通过, 错误说明)。"""
    if name not in data:
        return True, ''
    value = data[name]
    if value is None or value == '':
        return True, ''
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return False, f"{name} 必须是整数"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False, f"{name} 必须是整数"
    if not 1 <= n <= limit:
        return False, f"{name} 需在 1..{limit} 之间"
    return True, ''


def _valid_model_id(model_id: str) -> str:
    """校验模型 id,返回空串表示合法,否则返回错误说明。"""
    if len(model_id) > _ID_MAX_LEN:
        return f"id 超长(上限 {_ID_MAX_LEN} 字符)"
    if any(c in model_id for c in _ID_BAD_CHARS):
        return "id 不能包含路径分隔符"
    if model_id in ('.', '..') or model_id.startswith('.'):
        return "id 不能以 . 开头"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in model_id):
        return "id 不能包含控制字符"
    return ""


def _valid_model_url(url: str) -> str:
    """校验模型 url,返回空串表示合法,否则返回错误说明。"""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return "url 必须以 http:// 或 https:// 开头"
    if not parsed.netloc:
        return "url 缺少主机名"
    return ""


def _model_spec(data: dict) -> tuple[dict, str]:
    """
    把请求体收敛成 upsert_model 认识的 spec,返回 (spec, 错误说明)。
    只挑白名单字段,任何越界值都在这里挡下,不进 models.request。
    apiKey 显式传 null 表示清除本地 key,传空串表示保持原值,两者都要原样传下去。
    """
    if not isinstance(data, dict):
        return {}, "请求体必须是 JSON 对象"
    raw_id = data.get('id')
    model_id = raw_id.strip() if isinstance(raw_id, str) else ''
    if not model_id:
        return {}, "缺少 id"
    bad = _valid_model_id(model_id)
    if bad:
        return {}, bad

    for name, limit in (('name', _NAME_MAX_LEN), ('vendor', _NAME_MAX_LEN),
                        ('url', _URL_MAX_LEN), ('apiKeyEnv', _ENV_MAX_LEN),
                        ('apiKey', _KEY_MAX_LEN)):
        ok, why = _check_str(data, name, limit)
        if not ok:
            return {}, why
    for name, limit in (('maxInputTokens', _MAX_INPUT_TOKENS),
                        ('maxOutputTokens', _MAX_OUTPUT_TOKENS)):
        ok, why = _check_int(data, name, limit)
        if not ok:
            return {}, why

    url = (data.get('url') or '').strip() if isinstance(data.get('url'), str) else ''
    if url:
        bad = _valid_model_url(url)
        if bad:
            return {}, bad
    env_name = (data.get('apiKeyEnv') or '').strip() if isinstance(data.get('apiKeyEnv'), str) else ''
    if env_name and not _ENV_NAME_RE.match(env_name):
        return {}, "apiKeyEnv 只能是字母、数字与下划线,且不以数字开头"

    spec: dict = {"id": model_id}
    for name in ('name', 'vendor', 'url', 'apiKeyEnv', 'apiKey',
                 'maxInputTokens', 'maxOutputTokens'):
        if name in data:
            spec[name] = data[name]
    if 'supportsToolCall' in data:
        value = data['supportsToolCall']
        spec['supportsToolCall'] = None if value is None else _to_bool(value, False)
    return spec, ""


def _call_with_deadline(fn, timeout: float) -> tuple[bool, object, Exception | None]:
    """
    在守护线程里跑一个可能长时间阻塞的调用,返回 (是否按时完成, 返回值, 异常)。
    超时后不再等待,线程随进程退出,Flask 这一侧立刻能回 504。
    """
    box: dict = {}

    def runner():
        try:
            box['value'] = fn()
        except Exception as e:
            box['error'] = e

    t = threading.Thread(target=runner, name='model-test', daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None, None
    return True, box.get('value'), box.get('error')


@app.get('/api/models/admin')
def api_models_admin():
    """
    模型管理全表(含被 availableModels 隐藏的条目)。
    每项:{id, name, vendor, url, maxInputTokens, maxOutputTokens, supportsToolCall,
           hasKey, keySource, origin, editable},绝不含 apiKey。
    """
    return _safe_json({"ok": True, "models": list_models_admin()})


@app.post('/api/models/upsert')
def api_models_upsert():
    """
    新增或修改一个模型条目,只写 models/model.local.json。
    Body: {id, name?, vendor?, url?, apiKey?, apiKeyEnv?,
           maxInputTokens?, maxOutputTokens?, supportsToolCall?}
    apiKey 传空串=保持原值,传 null=清除本地 key。
    """
    data = flask_request.get_json(silent=True) or {}
    spec, why = _model_spec(data)
    if why:
        return _bad_request(why)
    res = upsert_model(spec)
    if not res.get("ok"):
        return _safe_json({"ok": False, "error": res.get("error", "写入失败"),
                           "code": "upsert_failed"}, 400)
    return _safe_json({"ok": True, "id": spec["id"], "models": list_models_admin()})


@app.post('/api/models/delete')
def api_models_delete():
    """
    删除本地覆盖层里的条目,model.json 里的条目不可删。
    Body: {"id": "<model_id>"}
    """
    data = flask_request.get_json(silent=True) or {}
    raw_id = data.get('id')
    model_id = raw_id.strip() if isinstance(raw_id, str) else ''
    if not model_id:
        return _bad_request("缺少 id")
    bad = _valid_model_id(model_id)
    if bad:
        return _bad_request(bad)
    res = delete_model(model_id)
    if not res.get("ok"):
        return _safe_json({"ok": False, "error": res.get("error", "删除失败"),
                           "code": "delete_failed"}, 400)
    return _safe_json({"ok": True, "id": model_id, "models": list_models_admin()})


@app.post('/api/models/test')
def api_models_test():
    """
    发一个最小请求验证该条目的 key 能不能用。
    Body: {"id": "<model_id>"}  →  {ok, latency_ms} / {ok:false, error, code}
    整个调用有 15 秒期限,超时返回 504,坏 URL 不会拖住 Flask 线程。
    """
    data = flask_request.get_json(silent=True) or {}
    raw_id = data.get('id')
    model_id = raw_id.strip() if isinstance(raw_id, str) else ''
    if not model_id:
        return _bad_request("缺少 id")
    bad = _valid_model_id(model_id)
    if bad:
        return _bad_request(bad)

    done, result, err = _call_with_deadline(
        lambda: test_model(model_id), _MODEL_TEST_TIMEOUT
    )
    if not done:
        return _safe_json({"ok": False, "code": "timeout",
                           "error": f"测试超时(超过 {int(_MODEL_TEST_TIMEOUT)} 秒)"}, 504)
    if err is not None:
        return _safe_json({"ok": False, "code": "test_failed",
                           "error": f"{type(err).__name__}: {err}"}, 502)
    if not isinstance(result, dict):
        return _safe_json({"ok": False, "code": "test_failed",
                           "error": "测试返回了非预期结构"}, 502)
    if not result.get("ok"):
        return _safe_json({"ok": False, "code": "test_failed",
                           "error": result.get("error", "测试失败")}, 502)
    return _safe_json({"ok": True, "id": model_id,
                       "latency_ms": result.get("latency_ms")})


@app.get('/api/folder')
def api_folder():
    """
    列目录。Query: ?path=,缺省用服务端探测出来的起始目录。
    返回里带 default_source 说明起始目录是怎么定的(sdcard / home)。
    """
    path = (flask_request.args.get('path') or '').strip()
    default_path, source = _default_root()
    if not path:
        path = default_path

    try:
        tree = list_dir(path)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except NotADirectoryError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except sandbox.SandboxError:
        raise
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    except OSError as e:
        return jsonify({"ok": False, "error": f"读取目录失败: {e}"}), 500

    return jsonify({
        "ok":             True,
        "platform":       "mobile" if source == 'sdcard' else "desktop",
        "default_path":   default_path,
        "default_source": source,
        "sdcard":         source == 'sdcard',
        "tree":           tree,
    })


# ════════════════════════════════════════════════════════════
#                      全局文件内容搜索
# ════════════════════════════════════════════════════════════

@app.get('/api/search')
def api_search():
    """全局搜索(子串,大小写不敏感)。Query: ?q=&path= """
    q    = (flask_request.args.get('q') or '').strip()
    path = (flask_request.args.get('path') or '').strip() or _default_root()[0]
    root = sandbox.resolve(path)
    try:
        result = file_search(root, q)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    result['ok'] = True
    return jsonify(result)


# ════════════════════════════════════════════════════════════
#                      应用配置查询与修改
# ════════════════════════════════════════════════════════════

@app.get('/api/config')
def api_config():
    """返回前端需要的全局开关(流式输出、轮数上限等),每次请求都按需重读配置文件。"""
    return jsonify({
        "ok":        True,
        "flow":      get_flow(),
        "max_round": get_max_round(),
        "path":      str(_CONFIG_PATH),
    })


@app.post('/api/config')
def api_config_set():
    """
    改全局开关并写回 .config.json(原子写,保留文件里的其它键)。
    Body: {"flow": bool?, "max_round": int?}
    """
    data = flask_request.get_json(silent=True) or {}
    patch = {}
    if "flow" in data:
        patch["flow"] = _to_bool(data["flow"], False)
    if "max_round" in data:
        try:
            n = int(data["max_round"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "max_round 必须是整数"}), 400
        if not 1 <= n <= 200:
            return jsonify({"ok": False, "error": "max_round 需在 1..200 之间"}), 400
        patch["max_round"] = n
    if not patch:
        return jsonify({"ok": False, "error": "没有可修改的字段"}), 400
    try:
        _write_config(patch)
    except OSError as e:
        return jsonify({"ok": False, "error": f"写入配置失败: {e}"}), 500
    return jsonify({"ok": True, "flow": get_flow(), "max_round": get_max_round()})


# ════════════════════════════════════════════════════════════
#                      Agent Chat
# ════════════════════════════════════════════════════════════

@app.get('/api/chat')
def api_chat():
    """GET /api/chat → 返回当前会话的 history + agent 状态(供前端初始化/刷新)。"""
    sess = _session()
    with sess.lock:
        history = list(sess.history)
    return jsonify({
        "ok":      True,
        "sid":     sess.id,
        "history": history,
        "state":   sess.snapshot(),
    })


@app.post('/api/chat')
def api_chat_send():
    """
    发送一条消息给 Agent,执行一轮 loop(可能多轮工具调用)。
    Body: {
        "message":    str,           # 用户输入(可空,纯续接)
        "history":    list|None,     # 可选:直接传完整 history,以前端为准
        "max_rounds": int,           # 缺省取 .config.json 的 max_round
        "plan_model": bool|None,     # None=使用会话当前值
        "cwd":        str|None,      # 前端资源管理器当前根目录
    }
    Returns: {ok, answer, rounds, stopped, tools_used, pending, history, state, sid}
    stopped ∈ answer | pending | max_rounds | error | cancelled
    """
    data = flask_request.get_json(silent=True) or {}
    sess = _session()
    history = _pick_history(sess, data)

    try:
        result = _run_agent(
            sess,
            user_message=(data.get("message") or "").strip(),
            history=history,
            max_rounds=int(data.get("max_rounds") or get_max_round()),
            plan_model=data.get("plan_model"),
            cwd=(data.get("cwd") or "").strip() or None,
        )
    except Exception as e:
        return jsonify({
            "ok":      False,
            "error":   f"{type(e).__name__}: {e}",
            "history": history,
            "sid":     sess.id,
        }), 500

    _save_history(sess, result.get("history") or history)
    return jsonify({
        "ok":         bool(result.get("ok")),
        "answer":     result.get("answer", ""),
        "rounds":     result.get("rounds", 0),
        "stopped":    result.get("stopped"),
        "tools_used": result.get("tools_used", []),
        "pending":    result.get("pending"),
        "history":    result.get("history", []),
        "state":      sess.snapshot(),
        "sid":        sess.id,
    })


@app.post('/api/chat/clear')
def api_chat_clear():
    """清空当前会话的 history,同时清掉 pending 与一次性授权。"""
    sess = _session()
    with sess.lock:
        sess.history = []
    sess.clear_pending()
    sess.clear_approval()
    _clear_cancel(sess.id)
    return jsonify({"ok": True, "history": [], "state": sess.snapshot(), "sid": sess.id})


@app.post('/api/chat/stop')
def api_chat_stop():
    """
    请求停止当前会话正在跑的 loop。
    置上取消标志,正在消费 run_stream 的请求会在下一个事件边界关闭生成器 ——
    也就是不会再发起下一轮模型调用,也不会再执行下一个工具。
    正在执行中的那一次工具调用无法从外部打断,会先跑完。
    """
    sess = _session()
    _mark_cancel(sess.id)
    return jsonify({"ok": True, "sid": sess.id, "cancelled": True})


# ════════════════════════════════════════════════════════════
#            Agent Chat 流式(SSE):实时显示每一步
# ════════════════════════════════════════════════════════════

@app.post('/api/chat/stream')
def api_chat_stream():
    """
    流式 chat 端点。把 run_stream 的事件序列化为 SSE 推给前端,
    让用户看到「第 N 轮 / 调用工具 X / 工具返回」。
    Body: 同 /api/chat。被 /api/chat/stop 停止时补发 cancelled + done 两个事件。
    """
    data = flask_request.get_json(silent=True) or {}
    sess = _session()
    history    = _pick_history(sess, data)
    user_msg   = (data.get("message") or "").strip()
    max_rounds = int(data.get("max_rounds") or get_max_round())
    plan_model = data.get("plan_model")
    cwd        = (data.get("cwd") or "").strip() or None
    use_flow   = _to_bool(data.get("flow"), get_flow()) if "flow" in data else get_flow()

    def sse(event: str, payload: dict) -> str:
        """把一个事件序列化成 SSE 帧。"""
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def gen():
        """消费 run_stream 并逐帧推给前端,每帧之后检查一次取消标志。"""
        _clear_cancel(sess.id)
        final = None
        rounds = 0
        tools_used: list[str] = []
        stream = agent_loop.run_stream(
            user_message=user_msg,
            history=history,
            max_rounds=max_rounds,
            plan_model=plan_model,
            cwd=cwd,
            use_flow=use_flow,
            sid=sess.id,
        )
        try:
            for ev in stream:
                kind = ev.pop("event", "message")
                if kind == "round":
                    rounds = ev.get("round", rounds)
                elif kind == "tool_call":
                    name = ev.get("name")
                    if name and name not in tools_used:
                        tools_used.append(name)
                yield sse(kind, ev)
                if kind == "done":
                    final = ev
                    break
                if _cancel_requested(sess.id):
                    final = _cancelled_result(sess, history, rounds, tools_used)
                    yield sse("cancelled", {"sid": sess.id, "rounds": rounds})
                    yield sse("done", final)
                    break
        except Exception as e:
            yield sse("error", {"message": f"{type(e).__name__}: {e}"})
            final = {
                "ok": False, "answer": f"{type(e).__name__}: {e}",
                "history": history, "rounds": rounds,
                "tools_used": tools_used, "pending": None, "stopped": "error",
                "sid": sess.id,
            }
        finally:
            stream.close()
            _clear_cancel(sess.id)
            if final:
                _save_history(sess, final.get("history") or history)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ════════════════════════════════════════════════════════════
#                      Agent State / Pending
# ════════════════════════════════════════════════════════════

@app.get('/api/agent/state')
def api_agent_state():
    """读取当前会话的 agent 状态:plan_model / auto / pending。"""
    sess = _session()
    return jsonify({"ok": True, "state": sess.snapshot(), "sid": sess.id})


@app.post('/api/agent/state')
def api_agent_state_set():
    """
    改当前会话的 agent 状态。
    Body: {"plan_model": bool?, "auto": bool?}
    """
    data = flask_request.get_json(silent=True) or {}
    sess = _session()
    with sess.lock:
        if "plan_model" in data:
            sess.plan_model = bool(data["plan_model"])
        if "auto" in data:
            sess.auto = bool(data["auto"])
    return jsonify({"ok": True, "state": sess.snapshot(), "sid": sess.id})


@app.post('/api/agent/pending/confirm')
def api_agent_pending_confirm():
    """
    确认 pending。
    把 pending 转成一次性授权(只对这一个动作、这一组参数放行一次),然后直接把 loop
    跑下去,不再依赖用户在聊天框里发"确认"两个字。
    Body: {"resume": bool=true, "cwd": str?, "max_rounds": int?, "plan_model": bool?}
    resume=false 时只授权不续跑,由前端自己再调 /api/chat/stream 续接。
    """
    data = flask_request.get_json(silent=True) or {}
    sess = _session()
    approved = sess.approve_pending()
    if approved is None:
        return jsonify({"ok": False, "error": "当前没有待确认的操作", "state": sess.snapshot()}), 400

    if not _to_bool(data.get("resume"), True):
        return jsonify({"ok": True, "approved": approved, "resumed": False,
                        "state": sess.snapshot(), "sid": sess.id})

    history = _pick_history(sess, data)
    try:
        result = _run_agent(
            sess,
            user_message="",
            history=history,
            max_rounds=int(data.get("max_rounds") or get_max_round()),
            plan_model=data.get("plan_model"),
            cwd=(data.get("cwd") or "").strip() or None,
        )
    except Exception as e:
        sess.clear_approval()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}",
                        "history": history, "sid": sess.id}), 500

    _save_history(sess, result.get("history") or history)
    return jsonify({
        "ok":         bool(result.get("ok")),
        "approved":   approved,
        "resumed":    True,
        "answer":     result.get("answer", ""),
        "rounds":     result.get("rounds", 0),
        "stopped":    result.get("stopped"),
        "tools_used": result.get("tools_used", []),
        "pending":    result.get("pending"),
        "history":    result.get("history", []),
        "state":      sess.snapshot(),
        "sid":        sess.id,
    })


@app.post('/api/agent/pending/reject')
def api_agent_pending_reject():
    """拒绝 pending:清空待确认项与一次性授权,模型需要重新发指令。"""
    sess = _session()
    sess.clear_pending()
    sess.clear_approval()
    return jsonify({"ok": True, "state": sess.snapshot(), "sid": sess.id})


# ════════════════════════════════════════════════════════════
#                      Diff 查看
# ════════════════════════════════════════════════════════════

@app.get('/api/diffs')
def api_diffs():
    """返回当前会话所有待展示的 diff 文件列表。"""
    return jsonify({"ok": True, "files": _session().diffs.list_pending()})


@app.get('/api/diff')
def api_diff():
    """返回单个文件的 unified diff + 行级类型(add/del/hunk/meta/ctx)。"""
    path = (flask_request.args.get('path') or '').strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    d = _session().diffs.get_diff(path)
    if not d:
        return jsonify({"ok": False, "error": "无 diff"}), 404
    return jsonify({"ok": True, **d})


@app.get('/api/diff/baseline')
def api_diff_baseline():
    """返回 agent 改动前的原文(用于编辑器红绿高亮基线)。"""
    path = (flask_request.args.get('path') or '').strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    baseline = _session().diffs.get_baseline(path)
    if baseline is None:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, "baseline": baseline})


@app.post('/api/diff/clear')
def api_diff_clear():
    """清空 diff(path=None 清全部)。Body: {"path": "..."} 或空"""
    data = flask_request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip() or None
    store = _session().diffs
    store.clear(path)
    return jsonify({"ok": True, "files": store.list_pending()})


@app.post('/api/diff/revert')
def api_diff_revert():
    """撤销单个文件的 agent 改动,恢复到改动前。Body: {"path": "..."}"""
    data = flask_request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    store = _session().diffs
    res = store.revert(path)
    if not res.get("ok"):
        return jsonify(res), 400
    return jsonify({"ok": True, "files": store.list_pending()})


@app.post('/api/diff/apply')
def api_diff_apply():
    """用户确认保留 → 把 pending patch 写入磁盘。Body: {"path": "..."}"""
    data = flask_request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    store = _session().diffs
    res = store.apply_patch(path)
    if not res.get("ok"):
        return jsonify(res), 409 if res.get("conflict") else 400
    return jsonify({"ok": True, "files": store.list_pending(), **res})


@app.post('/api/diff/discard')
def api_diff_discard():
    """用户撤销 → 丢弃 pending patch(不写磁盘)。Body: {"path": "..."}"""
    data = flask_request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    store = _session().diffs
    store.discard_patch(path)
    return jsonify({"ok": True, "files": store.list_pending()})


# ════════════════════════════════════════════════════════════
#                      Git 源代码管理(Tier 1)
# ════════════════════════════════════════════════════════════

@app.get('/api/git/status')
def api_git_status():
    """git status --porcelain=v1 -b → {branch, ahead, behind, changes: [...]}"""
    path = (flask_request.args.get('path') or '').strip() or _default_root()[0]
    cwd  = sandbox.resolve(path)
    if not os.path.isdir(cwd):
        return jsonify({"ok": False, "error": f"路径不存在: {cwd}"}), 400
    if not agent_git.is_repo(cwd):
        return jsonify({"ok": True, "is_git": False, "path": cwd})
    res = agent_git.status(cwd)
    if not res.ok:
        return jsonify({**res.as_dict(), "is_git": True, "path": cwd}), 400
    return jsonify({**res.as_dict(), "is_git": True, "path": cwd})


@app.get('/api/git/diff')
def api_git_diff():
    """git diff <path> 或 --staged,返回 unified diff 文本。"""
    path = (flask_request.args.get('path') or '').strip()
    cwd  = (flask_request.args.get('cwd')  or '').strip()
    staged = flask_request.args.get('staged') == '1'
    if not path or not cwd:
        return jsonify({"ok": False, "error": "缺少 path/cwd"}), 400
    res = agent_git.diff(sandbox.resolve(cwd), path, staged=staged)
    return jsonify({**res.as_dict(), "diff": res.output}), (200 if res.ok else 400)


@app.post('/api/git/stage')
def api_git_stage():
    """git add <path> 或 git add -A (path='-A')"""
    data = flask_request.get_json(silent=True) or {}
    got, missing = _need(data, 'cwd', 'path')
    if missing:
        return jsonify({"ok": False, "error": f"缺少 {missing}"}), 400
    cwd = sandbox.resolve(got['cwd'])
    res = agent_git.stage_all(cwd) if got['path'] == '-A' else agent_git.stage(cwd, got['path'])
    return jsonify({**res.as_dict(), "stderr": res.error}), (200 if res.ok else 400)


@app.post('/api/git/unstage')
def api_git_unstage():
    """git reset HEAD <path> 或 git reset(全撤)"""
    data = flask_request.get_json(silent=True) or {}
    cwd_raw = (data.get('cwd') or '').strip()
    path    = (data.get('path') or '').strip()
    if not cwd_raw:
        return jsonify({"ok": False, "error": "缺少 cwd"}), 400
    cwd = sandbox.resolve(cwd_raw)
    res = agent_git.unstage_all(cwd) if path in ('', '-A') else agent_git.unstage(cwd, path)
    return jsonify({**res.as_dict(), "stderr": res.error}), (200 if res.ok else 400)


@app.post('/api/git/discard')
def api_git_discard():
    """放弃工作区改动(对 modified 走 git checkout,对 untracked 走 git clean -f)"""
    data = flask_request.get_json(silent=True) or {}
    got, missing = _need(data, 'cwd', 'path')
    if missing:
        return jsonify({"ok": False, "error": f"缺少 {missing}"}), 400
    res = agent_git.discard(sandbox.resolve(got['cwd']), got['path'])
    return jsonify({**res.as_dict(), "stderr": res.error}), (200 if res.ok else 400)


@app.post('/api/git/commit')
def api_git_commit():
    """git commit -m <message>"""
    data = flask_request.get_json(silent=True) or {}
    got, missing = _need(data, 'cwd', 'message')
    if missing:
        return jsonify({"ok": False, "error": f"缺少 {missing}"}), 400
    res = agent_git.commit(sandbox.resolve(got['cwd']), got['message'])
    return jsonify(res.as_dict()), (200 if res.ok else 400)


@app.post('/api/git/push')
def api_git_push():
    """git push [remote [branch]],remote/branch 缺省时用当前 upstream。"""
    data   = flask_request.get_json(silent=True) or {}
    cwd    = (data.get('cwd')    or '').strip()
    remote = (data.get('remote') or '').strip()
    branch = (data.get('branch') or '').strip()
    if not cwd:
        return jsonify({"ok": False, "error": "缺少 cwd"}), 400
    res = agent_git.push(sandbox.resolve(cwd), remote=remote, branch=branch)
    return jsonify(res.as_dict()), (200 if res.ok else 400)


# ════════════════════════════════════════════════════════════
#                      文件与目录操作
# ════════════════════════════════════════════════════════════

@app.post('/api/file/create')
def api_create_file():
    """
    在指定目录下创建新文件(已存在 → 409)。
    Body: {"path": "<dir>", "name": "<filename>", "content": ""}
    """
    data = flask_request.get_json(silent=True) or {}
    got, missing = _need(data, 'path', 'name')
    if missing:
        return jsonify({"ok": False, "error": f"缺少 {missing}"}), 400
    content = data.get("content", "")
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "content 必须是字符串"}), 400
    full = sandbox.safe_join(got['path'], got['name'])
    if os.path.exists(full):
        return jsonify({"ok": False, "error": f"已存在: {got['name']}"}), 409
    try:
        file_create(full, content)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": full})


@app.post('/api/folder/create')
def api_create_folder():
    """
    在指定目录下创建新文件夹(已存在 → 409)。
    Body: {"path": "<dir>", "name": "<foldername>"}
    """
    data = flask_request.get_json(silent=True) or {}
    got, missing = _need(data, 'path', 'name')
    if missing:
        return jsonify({"ok": False, "error": f"缺少 {missing}"}), 400
    full = sandbox.safe_join(got['path'], got['name'])
    if os.path.exists(full):
        return jsonify({"ok": False, "error": f"已存在: {got['name']}"}), 409
    try:
        os.makedirs(full, exist_ok=False)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": full})


@app.get('/api/file/read')
def api_file_read():
    """
    读取文件原文。编码按 utf-8 -> utf-8-sig -> 本机 locale -> latin-1 探测,
    行尾探测出主导形式后随返回值一起给前端,保存时原样写回。
    content 统一成 LF 交给编辑器,真实行尾放在 newline 字段里。
    Query: ?path=<绝对路径>
    """
    path = (flask_request.args.get('path') or '').strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    full = sandbox.resolve(path)
    if not os.path.exists(full):
        return jsonify({"ok": False, "error": f"文件不存在: {full}"}), 404
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "不是文件(可能是目录)"}), 400
    try:
        size  = os.path.getsize(full)
        mtime = os.path.getmtime(full)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if size > MAX_TEXT_BYTES:
        return jsonify({
            "ok":    False,
            "code":  "too_large",
            "error": f"文件过大,超出 {MAX_TEXT_BYTES} 字节上限(实际 {size} 字节)",
            "size":  size,
            "limit": MAX_TEXT_BYTES,
        }), 413
    try:
        text, encoding = explorer_file.read_text_with_encoding(full)
    except ValueError as e:
        return jsonify({"ok": False, "code": "not_text", "error": str(e)}), 415
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({
        "ok":       True,
        "path":     full,
        "name":     os.path.basename(full),
        "size":     size,
        "mtime":    mtime,
        "encoding": encoding,
        "newline":  explorer_file.detect_newline(text),
        "content":  _norm_lf(text),
    })


@app.post('/api/file/save')
def api_file_save():
    """
    整体覆写文件,走 explorer 的原子写(临时文件 + fsync + os.replace)。
    encoding / newline 缺省沿用 /api/file/read 返回的值,不传则按 utf-8 + LF。
    content 里的行尾先归一成 LF 再按 newline 展开,避免 CRLF 被二次翻译成 \\r\\r\\n。
    Body: {"path": "<abs>", "content": "...", "encoding": "utf-8", "newline": "\\n"}
    """
    data = flask_request.get_json(silent=True) or {}
    path    = (data.get('path') or '').strip()
    content = data.get('content', '')
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "content 必须是字符串"}), 400
    encoding = (data.get('encoding') or 'utf-8').strip() or 'utf-8'
    newline  = data.get('newline') or '\n'
    if not _valid_encoding(encoding):
        return jsonify({"ok": False, "error": f"未知编码: {encoding}"}), 400
    if newline not in ('\n', '\r\n', '\r'):
        return jsonify({"ok": False, "error": "newline 只能是 \\n / \\r\\n / \\r"}), 400
    full = sandbox.resolve(path, write=True)
    if not os.path.exists(full):
        return jsonify({"ok": False, "error": f"文件不存在: {full}"}), 404
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "不是文件(可能是目录)"}), 400
    try:
        explorer_file.write_text(full, _norm_lf(content), encoding=encoding, newline=newline)
        size = os.path.getsize(full)
    except (OSError, UnicodeEncodeError) as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "path": full, "size": size,
                    "encoding": encoding, "newline": newline})


@app.post('/api/file/rename')
def api_file_rename():
    """
    重命名(同目录下换名,等价于 move)。
    Body: {"path": "<abs>", "new_name": "<filename without sep>"}
    """
    data = flask_request.get_json(silent=True) or {}
    got, missing = _need(data, 'path', 'new_name')
    if missing:
        return jsonify({"ok": False, "error": f"缺少 {missing}"}), 400
    full = sandbox.resolve(got['path'], write=True)
    if not os.path.exists(full):
        return jsonify({"ok": False, "error": f"源不存在: {full}"}), 404
    new_path = sandbox.safe_join(os.path.dirname(full), got['new_name'])
    if new_path == full:
        return jsonify({"ok": True, "path": full})
    if os.path.exists(new_path):
        return jsonify({"ok": False, "error": f"目标已存在: {got['new_name']}"}), 409
    try:
        os.rename(full, new_path)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": new_path})


@app.post('/api/file/duplicate')
def api_file_duplicate():
    """
    复制一份到同目录(自动加 _copy / _copy(N) 后缀,避免覆盖)。
    Body: {"path": "<abs>"}  →  返回新文件路径。
    """
    data = flask_request.get_json(silent=True) or {}
    path = (data.get('path') or '').strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    full = sandbox.resolve(path)
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "不是文件(可能是目录)"}), 400
    base, ext = os.path.splitext(full)
    parent    = os.path.dirname(full)
    bare      = os.path.basename(base)
    new_path  = sandbox.safe_join(parent, f"{bare}_copy{ext}")
    n = 2
    while os.path.exists(new_path):
        new_path = sandbox.safe_join(parent, f"{bare}_copy({n}){ext}")
        n += 1
    try:
        import shutil
        shutil.copy2(full, new_path)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": new_path})


@app.post('/api/file/move')
def api_file_move():
    """
    移动文件到目标目录(支持重命名)。
    Body: {"path": "<abs>", "to_dir": "<abs dir>", "new_name": "<filename> (optional)"}
    """
    data = flask_request.get_json(silent=True) or {}
    got, missing = _need(data, 'path', 'to_dir')
    if missing:
        return jsonify({"ok": False, "error": f"缺少 {missing}"}), 400
    full   = sandbox.resolve(got['path'], write=True)
    to_dir = sandbox.resolve(got['to_dir'])
    new_n  = (data.get('new_name') or '').strip() or os.path.basename(full)
    if not os.path.exists(full):
        return jsonify({"ok": False, "error": f"源不存在: {full}"}), 404
    if not os.path.isdir(to_dir):
        return jsonify({"ok": False, "error": f"目标目录不存在: {to_dir}"}), 400
    new_path = sandbox.safe_join(to_dir, new_n)
    if os.path.exists(new_path):
        return jsonify({"ok": False, "error": f"目标已存在: {new_n}"}), 409
    try:
        os.rename(full, new_path)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": new_path})


@app.post('/api/file/delete')
def api_file_delete():
    """
    删除文件(仅限文件,目录请用 /api/folder/delete)。
    Body: {"path": "<abs>"}
    """
    data = flask_request.get_json(silent=True) or {}
    path = (data.get('path') or '').strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    full = sandbox.resolve(path, write=True)
    if not os.path.exists(full):
        return jsonify({"ok": False, "error": f"文件不存在: {full}"}), 404
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "不是文件(可能是目录)"}), 400
    try:
        os.remove(full)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": full})


# ════════════════════════════════════════════════════════════
#                      SSH 远程会话
# ════════════════════════════════════════════════════════════

@app.post('/api/ssh/connect')
def api_ssh_connect():
    """
    新建 SSH 连接,返回 sid。Body: {host, port, user, password?, key_path?}
    主机指纹未知或不匹配时返回 409 + 待确认结构,由 /api/ssh/host_key/confirm 处理。
    """
    from ssh import get_manager, SshHostKeyError
    data = flask_request.get_json(silent=True) or {}
    host     = (data.get('host')     or '').strip()
    user     = (data.get('user')     or '').strip()
    password = (data.get('password') or '')
    key_path = (data.get('key_path') or '').strip()
    try:
        port = int(data.get('port') or 22)
    except (TypeError, ValueError):
        port = 22
    if not host:
        return jsonify({"ok": False, "error": "缺少 host"}), 400
    if not user:
        return jsonify({"ok": False, "error": "缺少 user"}), 400
    if not password and not key_path:
        return jsonify({"ok": False, "error": "必须提供 password 或 key_path"}), 400
    try:
        s = get_manager().create(host, port, user, password=password, key_path=key_path)
    except SshHostKeyError as e:
        return jsonify(e.detail), 409
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "session": s.info()})


@app.post('/api/ssh/host_key/confirm')
def api_ssh_host_key_confirm():
    """
    用户核对指纹后接受:把公钥写进 known_hosts。Body: {token}
    写入后需要前端重新调一次 /api/ssh/connect(票据里不缓存密码)。
    """
    from ssh import get_manager
    data  = flask_request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()
    if not token:
        return jsonify({"ok": False, "error": "缺少 token"}), 400
    res = get_manager().confirm_host_key(token)
    return jsonify(res), (200 if res.get('ok') else 400)


@app.post('/api/ssh/host_key/cancel')
def api_ssh_host_key_cancel():
    """用户拒绝指纹:丢弃待确认票据。Body: {token}"""
    from ssh import get_manager
    data  = flask_request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()
    if not token:
        return jsonify({"ok": False, "error": "缺少 token"}), 400
    return jsonify(get_manager().cancel_host_key(token))


@app.get('/api/ssh/sessions')
def api_ssh_sessions():
    """列出所有活动 SSH 会话。"""
    from ssh import get_manager
    return jsonify({"ok": True, "sessions": get_manager().list()})


@app.post('/api/ssh/disconnect')
def api_ssh_disconnect():
    """关闭指定 SSH 会话。Body: {sid}"""
    from ssh import get_manager
    data = flask_request.get_json(silent=True) or {}
    sid  = (data.get('sid') or '').strip()
    if not sid:
        return jsonify({"ok": False, "error": "缺少 sid"}), 400
    try:
        ok = get_manager().destroy(sid)
    except RuntimeError as e:
        return jsonify({"ok": False, "sid": sid, "error": str(e)}), 500
    return jsonify({"ok": ok, "sid": sid})


@app.get('/api/ssh/list')
def api_ssh_list():
    """列远程目录。Query: ?sid=...&path=..."""
    from ssh import get_manager
    sid  = (flask_request.args.get('sid')  or '').strip()
    path = (flask_request.args.get('path') or '').strip() or '.'
    s = get_manager().get(sid)
    if not s:
        return jsonify({"ok": False, "error": "session not found"}), 404
    return jsonify(s.list_dir(path))


@app.post('/api/ssh/exec')
def api_ssh_exec():
    """远端跑一条命令。Body: {sid, command, cwd?, timeout?}"""
    from ssh import get_manager
    data = flask_request.get_json(silent=True) or {}
    sid     = (data.get('sid') or '').strip()
    command = (data.get('command') or '').strip()
    cwd     = (data.get('cwd') or '').strip()
    try:
        timeout = int(data.get('timeout') or 30)
    except (TypeError, ValueError):
        timeout = 30
    if not sid or not command:
        return jsonify({"ok": False, "error": "缺少 sid/command"}), 400
    s = get_manager().get(sid)
    if not s:
        return jsonify({"ok": False, "error": "session not found"}), 404
    return jsonify(s.exec(command, cwd=cwd, timeout=timeout))


# ════════════════════════════════════════════════════════════
#                      终端执行
# ════════════════════════════════════════════════════════════

@app.post('/api/terminal/run')
def api_terminal_run():
    """
    执行一条 shell 命令并返回结果。
    Body: {"command": "...", "cwd": "..."(可选), "timeout": int(秒,可选,默认 30),
           "ssh_sid": "..."(可选,传了就走 SSH 远程执行)}
    本地执行时 cwd 先过 sandbox;走 SSH 时 cwd 属于远端文件系统,不做本地校验。
    """
    data    = flask_request.get_json(silent=True) or {}
    command = (data.get('command') or '').strip()
    cwd     = (data.get('cwd') or '').strip() or None
    ssh_sid = (data.get('ssh_sid') or '').strip()
    try:
        timeout = int(data.get('timeout') or 30)
    except (TypeError, ValueError):
        timeout = 30
    if not command:
        return jsonify({"ok": False, "error": "缺少 command"}), 400

    if ssh_sid:
        from ssh import get_manager
        s = get_manager().get(ssh_sid)
        if not s:
            return jsonify({"ok": False, "error": "ssh session not found"}), 404
        return jsonify(s.exec(command, cwd=cwd or '', timeout=timeout))

    from terminal import run as term_run
    return jsonify(term_run(command, cwd=sandbox.resolve(cwd) if cwd else None, timeout=timeout))


# ════════════════════════════════════════════════════════════
#                          启动
# ════════════════════════════════════════════════════════════

def main() -> None:
    """解析命令行参数,打印带 token 的访问地址,然后起服务。"""
    import argparse
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument('--host', default=os.environ.get('CODEFORGE_HOST', '127.0.0.1'))
    p.add_argument('--port', type=int,
                   default=int(os.environ.get('CODEFORGE_PORT', str(DEFAULT_PORT))))
    p.add_argument('--debug', action='store_true',
                   default=os.environ.get('CODEFORGE_DEBUG', '0') in ('1', 'true', 'True'))
    a = p.parse_args()

    auth.configure(port=a.port, extra_hosts=[a.host])
    print(auth.banner(a.host, a.port), flush=True)
    app.run(host=a.host, port=a.port, debug=a.debug, use_reloader=False)


if __name__ == '__main__':
    main()
