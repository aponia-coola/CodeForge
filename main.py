import os

from flask import Flask, jsonify, request as flask_request

from explorer.file import list_dir, create as file_create
from models.request import (
    get_models,
    get_current_model,
    get_base_url,
    set_current_model,
)

app = Flask(__name__)


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
    return jsonify({"ok": True})


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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9191, debug=False)
