from flask import Flask, jsonify, request as flask_request

from models.request import (
    get_models,
    get_current_model,
    get_base_url,
    set_current_model,
)

app = Flask(__name__)


# ============ 全局:禁止浏览器缓存静态文件 ============
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9191, debug=False)
