import os

from flask import Flask, jsonify, request as flask_request

from explorer.file import list_dir, create as file_create
from models.request import (
    get_models,
    get_current_model,
    get_base_url,
    set_current_model,
)
from agent import state as agent_state
from agent import loop as agent_loop

app = Flask(__name__)


# ════════════════════════════════════════════════════════════
#                      Agent 历史(内存会话)
# ════════════════════════════════════════════════════════════
# 简单进程内存储:UI 重启就清空;够用。
_HISTORY: list[dict] = []


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


# ============ 页面 ============
@app.route('/')
def index():
    return app.send_static_file('index.html')


# ============ API ============
@app.get('/api/models')
def api_models():
    """返回模型列表 + 当前激活的模型。"""
    return jsonify({
        "current": get_current_model(),
        "models": get_models(),
    })


@app.post('/api/model')
def api_switch_model():
    """
    切换当前模型。
    Body: {"model": "<model_id>"}
    Returns: {"ok": true, "current": "..."}  /  {"ok": false, "error": "..."}
    """
    data = flask_request.get_json(silent=True) or {}
    model_id = data.get("model", "").strip()
    if not model_id:
        return jsonify({"ok": False, "error": "缺少 model 字段"}), 400
    if set_current_model(model_id):
        return jsonify({"ok": True, "current": get_current_model()})
    return jsonify({"ok": False, "error": f"未知模型: {model_id}"}), 404

_MOBILE_UA_KEYWORDS = (
    'android', 'iphone', 'ipad', 'ipod', 'mobile', 'webos', 'opera mini',
)
_MOBILE_DEFAULT_DIR = '/sdcard'


def _is_mobile_user_agent(ua: str) -> bool:
    """根据 User-Agent 头粗略判断是否为移动端。"""
    ua = (ua or '').lower()
    return any(k in ua for k in _MOBILE_UA_KEYWORDS)


@app.get('/api/folder')
def api_folder():
    path = (flask_request.args.get('path') or '').strip()
    ua = flask_request.headers.get('User-Agent', '')
    is_mobile = _is_mobile_user_agent(ua)

    if not path:
        if is_mobile:
            path = _MOBILE_DEFAULT_DIR
        else:
            path = os.path.expanduser('~')

    try:
        tree = list_dir(path)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except NotADirectoryError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    except OSError as e:
        return jsonify({"ok": False, "error": f"读取目录失败: {e}"}), 500

    return jsonify({
        "ok": True,
        "platform": "mobile" if is_mobile else "desktop",
        "default_path": _MOBILE_DEFAULT_DIR if is_mobile else os.path.expanduser('~'),
        "tree": tree,
    })

@app.get('/api/chat')
def api_chat():
    """GET /api/chat → 返回当前 history + agent 状态(供前端初始化/刷新)。"""
    return jsonify({
        "ok":      True,
        "history": _HISTORY,
        "state":   agent_state.snapshot(),
    })


# ════════════════════════════════════════════════════════════
#                      Agent Chat
# ════════════════════════════════════════════════════════════

@app.post('/api/chat')
def api_chat_send():
    """
    发送一条消息给 Agent,执行一轮 loop(可能多轮工具调用)。
    Body: {
        "message":    str,           # 用户输入(可空,纯续接)
        "history":    list|None,     # 可选:直接传完整 history,以前端为准
        "max_rounds": int = 10,
        "plan_model": bool|None,     # None=使用 state 当前值
    }
    Returns: {
        "ok":        bool,
        "answer":    str,
        "rounds":    int,
        "stopped":   "answer" | "pending" | "max_rounds" | "error",
        "tools_used": [str,...],
        "pending":   dict|None,
        "history":   list,
    }
    """
    data = flask_request.get_json(silent=True) or {}
    global _HISTORY

    # history 优先用 body 传的(前端全权管理),否则用进程内的
    if "history" in data and isinstance(data["history"], list):
        history = data["history"]
    else:
        history = _HISTORY

    user_message = (data.get("message") or "").strip()
    max_rounds   = int(data.get("max_rounds") or 10)
    plan_model   = data.get("plan_model")  # None / True / False
    cwd          = (data.get("cwd") or "").strip() or None  # 前端资源管理器当前根目录(可选)

    try:
        result = agent_loop.run(
            user_message=user_message,
            history=history,
            max_rounds=max_rounds,
            plan_model=plan_model,
            cwd=cwd,
        )
    except Exception as e:
        return jsonify({
            "ok":      False,
            "error":   f"{type(e).__name__}: {e}",
            "history": history,
        }), 500

    # 把 loop 跑完的最新 history 存回进程内(供后续 GET)
    _HISTORY = result.get("history") or history

    return jsonify({
        "ok":         True,
        "answer":     result.get("answer", ""),
        "rounds":     result.get("rounds", 0),
        "stopped":    result.get("stopped"),
        "tools_used": result.get("tools_used", []),
        "pending":    result.get("pending"),
        "history":    result.get("history", []),
        "state":      agent_state.snapshot(),
    })


@app.post('/api/chat/clear')
def api_chat_clear():
    """清空 history,同时清掉 pending。"""
    global _HISTORY
    _HISTORY = []
    agent_state.clear_pending()
    return jsonify({"ok": True, "history": [], "state": agent_state.snapshot()})


# ════════════════════════════════════════════════════════════
#                      Agent State / Pending
# ════════════════════════════════════════════════════════════

@app.get('/api/agent/state')
def api_agent_state():
    """读取 agent 状态:plan_model / auto / pending。"""
    return jsonify({"ok": True, "state": agent_state.snapshot()})


@app.post('/api/agent/state')
def api_agent_state_set():
    """
    改 agent 状态。
    Body: {"plan_model": bool?, "auto": bool?}
    """
    data = flask_request.get_json(silent=True) or {}
    if "plan_model" in data:
        agent_state.set_plan_model(bool(data["plan_model"]))
    if "auto" in data:
        agent_state.set_auto(bool(data["auto"]))
    return jsonify({"ok": True, "state": agent_state.snapshot()})


@app.post('/api/agent/pending/confirm')
def api_agent_pending_confirm():
    """
    确认 pending(在 chat 中输入"确认"后,会触发 loop 自己走完)。
    当前实现:清空 pending 状态,让前端的 pending 卡片立即关闭;
    实际继续执行由 chat 里追加的"确认"消息驱动(loop.py 会临时开 auto)。
    """
    agent_state.clear_pending()
    return jsonify({"ok": True, "state": agent_state.snapshot()})


@app.post('/api/agent/pending/reject')
def api_agent_pending_reject():
    """拒绝 pending:直接清空(模型需要重新发指令)。"""
    agent_state.clear_pending()
    return jsonify({"ok": True, "state": agent_state.snapshot()})


@app.post('/api/file/create')
def api_create_file():
    """
    在指定目录下创建新文件(已存在 → 409)。
    Body: {"path": "<dir>", "name": "<filename>", "content": ""}
    """
    data = flask_request.get_json(silent=True) or {}
    parent = (data.get("path") or "").strip()
    name   = (data.get("name") or "").strip()
    content = data.get("content", "")
    if not parent or not name:
        return jsonify({"ok": False, "error": "缺少 path 或 name"}), 400
    if "/" in name or "\\" in name:
        return jsonify({"ok": False, "error": "name 不能含路径分隔符"}), 400
    full = os.path.join(parent, name)
    try:
        if os.path.exists(full):
            return jsonify({"ok": False, "error": f"已存在: {name}"}), 409
        file_create(full)
        if content:
            from explorer.file import change
            change(full, content, mode="append")
        return jsonify({"ok": True, "path": full})
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post('/api/folder/create')
def api_create_folder():
    """
    在指定目录下创建新文件夹(已存在 → 409)。
    Body: {"path": "<dir>", "name": "<foldername>"}
    """
    data = flask_request.get_json(silent=True) or {}
    parent = (data.get("path") or "").strip()
    name   = (data.get("name") or "").strip()
    if not parent or not name:
        return jsonify({"ok": False, "error": "缺少 path 或 name"}), 400
    if "/" in name or "\\" in name:
        return jsonify({"ok": False, "error": "name 不能含路径分隔符"}), 400
    full = os.path.join(parent, name)
    try:
        if os.path.exists(full):
            return jsonify({"ok": False, "error": f"已存在: {name}"}), 409
        os.makedirs(full, exist_ok=False)
        return jsonify({"ok": True, "path": full})
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get('/api/file/read')
def api_file_read():
    """
    读取文件原文(走 utf-8,失败字符用 U+FFFD 替代)。
    Query: ?path=<绝对路径>
    """
    path = (flask_request.args.get('path') or '').strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": f"文件不存在: {path}"}), 404
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "不是文件(可能是目录)"}), 400
    try:
        size  = os.path.getsize(path)
        mtime = os.path.getmtime(path)
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return jsonify({
            "ok":      True,
            "path":    path,
            "name":    os.path.basename(path),
            "size":    size,
            "mtime":   mtime,
            "content": content,
        })
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post('/api/file/save')
def api_file_save():
    """
    整体覆写文件(utf-8)。
    Body: {"path": "<abs>", "content": "..."}
    """
    data = flask_request.get_json(silent=True) or {}
    path    = (data.get('path') or '').strip()
    content = data.get('content', '')
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": f"文件不存在: {path}"}), 404
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "不是文件(可能是目录)"}), 400
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        size = os.path.getsize(path)
        return jsonify({"ok": True, "path": path, "size": size})
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ════════════════════════════════════════════════════════════
#                      终端执行
# ════════════════════════════════════════════════════════════

@app.post('/api/terminal/run')
def api_terminal_run():
    """
    执行一条 shell 命令并返回结果。
    Body: {"command": "...", "cwd": "..."(可选), "timeout": int(秒,可选,默认 30)}
    """
    from terminal import run as term_run
    data    = flask_request.get_json(silent=True) or {}
    command = (data.get('command') or '').strip()
    cwd     = (data.get('cwd') or '').strip() or None
    try:
        timeout = int(data.get('timeout') or 30)
    except (TypeError, ValueError):
        timeout = 30
    if not command:
        return jsonify({"ok": False, "error": "缺少 command"}), 400
    result = term_run(command, cwd=cwd, timeout=timeout)
    return jsonify(result)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9191, debug=False)
