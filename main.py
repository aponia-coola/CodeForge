import json
import os
from pathlib import Path

from flask import Flask, jsonify, request as flask_request, Response

from explorer.file import list_dir, create as file_create
from explorer.search import search as file_search
from models.request import (
    get_models,
    get_current_model,
    get_base_url,
    set_current_model,
)
from agent import state as agent_state
from agent import loop as agent_loop
from agent import diff as agent_diff
from git import engine as agent_git

app = Flask(__name__)


# ════════════════════════════════════════════════════════════
#                      应用配置(.config.json)
# ════════════════════════════════════════════════════════════

_CONFIG_PATH = Path(__file__).resolve().parent / ".config.json"

def _read_app_config() -> dict:
    """读 .config.json,出错/缺字段则给默认。"""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

_APP_CFG = _read_app_config()

def _to_bool(v, default: bool = False) -> bool:
    """容错地把 'true'/'false' 字符串 / bool 转成 bool。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ('true', '1', 'yes', 'on')
    if v is None:
        return default
    return bool(v)

FLOW: bool     = _to_bool(_APP_CFG.get('flow'),  False)
MAX_ROUND_CFG: int = int(_APP_CFG.get('max_round', 20))


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
    """
    返回模型列表 + 当前激活的模型。
    只刷新模型列表(支持新增/删除),不动 current_model,这样用户的热切换选择不会被覆盖。
    """
    from models.request import reload_models_list
    reload_models_list()
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
#                      全局文件内容搜索
# ════════════════════════════════════════════════════════════

@app.get('/api/search')
def api_search():
    """全局搜索(子串,大小写不敏感)。Query: ?q=&path= """
    q    = (flask_request.args.get('q') or '').strip()
    path = (flask_request.args.get('path') or '').strip() or os.path.expanduser('~')
    try:
        result = file_search(path, q)
        result['ok'] = True
        return jsonify(result)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ════════════════════════════════════════════════════════════
#                      应用配置查询
# ════════════════════════════════════════════════════════════

@app.get('/api/config')
def api_config():
    """返回前端需要的全局开关(流式输出、轮数上限等)。"""
    return jsonify({
        "ok":       True,
        "flow":     FLOW,
        "max_round": MAX_ROUND_CFG,
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
#            Agent Chat 流式(SSE):实时显示每一步
# ════════════════════════════════════════════════════════════

@app.post('/api/chat/stream')
def api_chat_stream():
    """
    流式 chat 端点。把 run_stream 的事件序列化为 SSE 推给前端,
    让用户看到「第 N 轮 / 调用工具 X / 工具返回」。
    Body: 同 /api/chat
    """
    import json as _json
    data = flask_request.get_json(silent=True) or {}
    history    = data.get("history") if isinstance(data.get("history"), list) else _HISTORY
    user_msg   = (data.get("message") or "").strip()
    max_rounds = int(data.get("max_rounds") or 20)
    plan_model = data.get("plan_model")
    cwd        = (data.get("cwd") or "").strip() or None
    use_flow   = bool(data.get("flow", FLOW))  # body 缺省取 .config.json 的 FLOW

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {_json.dumps(payload, ensure_ascii=False)}\n\n"

    def gen():
        # 用 list 临时缓存 done 事件,最后同步给 _HISTORY
        final = None
        try:
            for ev in agent_loop.run_stream(
                user_message=user_msg,
                history=history,
                max_rounds=max_rounds,
                plan_model=plan_model,
                cwd=cwd,
                use_flow=use_flow,
            ):
                kind = ev.pop("event", "message")
                if kind == "done":
                    final = ev
                yield sse(kind, ev)
        except Exception as e:
            yield sse("error", {"message": f"{type(e).__name__}: {e}"})
            final = {
                "ok": False, "answer": f"{type(e).__name__}: {e}",
                "history": history, "rounds": 0,
                "tools_used": [], "pending": None, "stopped": "error",
            }

        # 流结束,把 done 状态写回进程内 history
        global _HISTORY
        if final:
            _HISTORY = final.get("history") or history

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ════════════════════════════════════════════════════════════
#                      Agent State / Pending
# ════════════════════════════════════════════════════════════

@app.get('/api/agent/state')
def api_agent_state():
    """读取 agent 状态:plan_model / auto / pending。"""
    return jsonify({"ok": True, "state": agent_state.snapshot()})


# ════════════════════════════════════════════════════════════
#                      Diff 查看
# ════════════════════════════════════════════════════════════

@app.get('/api/diffs')
def api_diffs():
    """返回所有待展示的 diff 文件列表。"""
    return jsonify({"ok": True, "files": agent_diff.list_pending()})


@app.get('/api/diff')
def api_diff():
    """返回单个文件的 unified diff + 行级类型(add/del/hunk/meta/ctx)。"""
    path = (flask_request.args.get('path') or '').strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    d = agent_diff.get_diff(path)
    if not d:
        return jsonify({"ok": False, "error": "无 diff"}), 404
    return jsonify({"ok": True, **d})


@app.get('/api/diff/baseline')
def api_diff_baseline():
    """返回 agent 改动前的原文(用于编辑器红绿高亮基线)。"""
    path = (flask_request.args.get('path') or '').strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    baseline = agent_diff.get_baseline(path)
    if baseline is None:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, "baseline": baseline})


@app.post('/api/diff/clear')
def api_diff_clear():
    """清空 diff(path=None 清全部)。Body: {"path": "..."} 或空"""
    data = flask_request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip() or None
    agent_diff.clear(path)
    return jsonify({"ok": True, "files": agent_diff.list_pending()})


@app.post('/api/diff/revert')
def api_diff_revert():
    """撤销单个文件的 agent 改动,恢复到改动前。Body: {"path": "..."}"""
    data = flask_request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    res = agent_diff.revert(path)
    if not res.get("ok"):
        return jsonify(res), 400
    return jsonify({"ok": True, "files": agent_diff.list_pending()})


@app.post('/api/diff/apply')
def api_diff_apply():
    """用户确认保留 → 把 pending patch 写入磁盘。Body: {"path": "..."}"""
    data = flask_request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    res = agent_diff.apply_patch(path)
    if not res.get("ok"):
        return jsonify(res), 400
    return jsonify({"ok": True, "files": agent_diff.list_pending()})


@app.post('/api/diff/discard')
def api_diff_discard():
    """用户撤销 → 丢弃 pending patch(不写磁盘)。Body: {"path": "..."}"""
    data = flask_request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    agent_diff.discard_patch(path)
    return jsonify({"ok": True, "files": agent_diff.list_pending()})


# ════════════════════════════════════════════════════════════
#                      Git 源代码管理(Tier 1)
# ════════════════════════════════════════════════════════════

@app.get('/api/git/status')
def api_git_status():
    """git status --porcelain=v1 -b → {branch, ahead, behind, changes: [...]}"""
    path = (flask_request.args.get('path') or '').strip() or os.path.expanduser('~')
    if not os.path.isdir(path):
        return jsonify({"ok": False, "error": f"路径不存在: {path}"}), 400
    if not agent_git.is_repo(path):
        return jsonify({"ok": True, "is_git": False, "path": path})
    try:
        s = agent_git.status(path)
        return jsonify({"ok": True, "is_git": True, "path": path, **s})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get('/api/git/diff')
def api_git_diff():
    """git diff <path> 或 --staged,返回 unified diff 文本。"""
    path = (flask_request.args.get('path') or '').strip()
    cwd  = (flask_request.args.get('cwd')  or '').strip()
    staged = flask_request.args.get('staged') == '1'
    if not path or not cwd:
        return jsonify({"ok": False, "error": "缺少 path/cwd"}), 400
    try:
        text = agent_git.diff(cwd, path, staged=staged)
        return jsonify({"ok": True, "diff": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post('/api/git/stage')
def api_git_stage():
    """git add <path> 或 git add -A (path='-A')"""
    data = flask_request.get_json(silent=True) or {}
    cwd  = (data.get('cwd')  or '').strip()
    path = (data.get('path') or '').strip()
    if not cwd or not path:
        return jsonify({"ok": False, "error": "缺少 cwd/path"}), 400
    if path == '-A':
        p = subprocess_run_git(cwd, 'add', '-A')
    else:
        p = agent_git.stage(cwd, path)
    return jsonify({"ok": p[0] if isinstance(p, tuple) else (p.returncode == 0),
                    "stderr": p[1] if isinstance(p, tuple) else (p.stderr or '').strip()})


@app.post('/api/git/unstage')
def api_git_unstage():
    """git reset HEAD <path> 或 git reset(全撤)"""
    data = flask_request.get_json(silent=True) or {}
    cwd  = (data.get('cwd')  or '').strip()
    path = (data.get('path') or '').strip()
    if not cwd:
        return jsonify({"ok": False, "error": "缺少 cwd"}), 400
    if path == '-A' or not path:
        ok, err = _git_reset_all(cwd)
    else:
        ok, err = agent_git.unstage(cwd, path)
    return jsonify({"ok": ok, "stderr": err})


@app.post('/api/git/discard')
def api_git_discard():
    """放弃工作区改动(对 modified 走 git checkout,对 untracked 走 git clean -f)"""
    data = flask_request.get_json(silent=True) or {}
    cwd  = (data.get('cwd')  or '').strip()
    path = (data.get('path') or '').strip()
    if not cwd or not path:
        return jsonify({"ok": False, "error": "缺少 cwd/path"}), 400
    ok, err = agent_git.discard(cwd, path)
    return jsonify({"ok": ok, "stderr": err})


@app.post('/api/git/commit')
def api_git_commit():
    """git commit -m <message>"""
    data = flask_request.get_json(silent=True) or {}
    cwd     = (data.get('cwd')     or '').strip()
    message = (data.get('message') or '').strip()
    if not cwd or not message:
        return jsonify({"ok": False, "error": "缺少 cwd/message"}), 400
    ok, out, h = agent_git.commit(cwd, message)
    return jsonify({"ok": ok, "output": out, "hash": h})


@app.post('/api/git/push')
def api_git_push():
    """git push [remote [branch]],remote/branch 缺省时用当前 upstream。"""
    data    = flask_request.get_json(silent=True) or {}
    cwd     = (data.get('cwd')     or '').strip()
    remote  = (data.get('remote')  or '').strip()
    branch  = (data.get('branch')  or '').strip()
    if not cwd:
        return jsonify({"ok": False, "error": "缺少 cwd"}), 400
    ok, out = agent_git.push(cwd, remote=remote, branch=branch)
    return jsonify({"ok": ok, "output": out})


def subprocess_run_git(cwd, *args):
    """包装 subprocess.run 给上面的 stage(-A) 用。"""
    import subprocess
    p = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=15)
    return (p.returncode == 0, (p.stderr or '').strip())


def _git_reset_all(cwd):
    import subprocess
    p = subprocess.run(['git', 'reset'], cwd=cwd, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=15)
    return (p.returncode == 0, (p.stderr or '').strip())


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
#                      SSH 远程会话
# ════════════════════════════════════════════════════════════

@app.post('/api/ssh/connect')
def api_ssh_connect():
    """新建 SSH 连接,返回 sid。Body: {host, port, user, password?, key_path?}"""
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
        from ssh import get_manager
        s = get_manager().create(host, port, user, password=password, key_path=key_path)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "session": s.info()})


@app.get('/api/ssh/sessions')
def api_ssh_sessions():
    """列出所有活动 SSH 会话。"""
    from ssh import get_manager
    return jsonify({"ok": True, "sessions": get_manager().list()})


@app.post('/api/ssh/disconnect')
def api_ssh_disconnect():
    """关闭指定 SSH 会话。Body: {sid}"""
    data = flask_request.get_json(silent=True) or {}
    sid  = (data.get('sid') or '').strip()
    if not sid:
        return jsonify({"ok": False, "error": "缺少 sid"}), 400
    from ssh import get_manager
    ok = get_manager().destroy(sid)
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
    from ssh import get_manager
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

    # 走 SSH
    if ssh_sid:
        from ssh import get_manager
        s = get_manager().get(ssh_sid)
        if not s:
            return jsonify({"ok": False, "error": "ssh session not found"}), 404
        return jsonify(s.exec(command, cwd=cwd or '', timeout=timeout))

    # 本地
    from terminal import run as term_run
    result = term_run(command, cwd=cwd, timeout=timeout)
    return jsonify(result)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9191, debug=True)
