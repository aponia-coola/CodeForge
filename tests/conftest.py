"""
pytest 公共夹具。

三条硬约束:
1. 绝不发起真实网络 / LLM 调用。no_network 是 autouse 的,把模型层的四个入口
   全部换成会抛异常的桩,任何漏网的调用都会立刻炸掉而不是偷偷走网络。
2. 绝不写进仓库。tmp_workspace 把 sandbox 的根目录整个换成 pytest 的临时目录,
   越界写在 sandbox.resolve() 这一层就被拒,不依赖用例自觉。
3. 全局状态每个用例重置。agent.session 的会话表、diff 池、sandbox 根目录都在
   夹具退出时还原,用例之间不互相污染。

token 必须在 import auth 之前写进环境变量:auth.token() 是懒加载并缓存的,
晚一步就会拿到随机生成的值,测试再也对不上。
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TEST_TOKEN = "codeforge-test-token"
os.environ.setdefault("CODEFORGE_TOKEN", TEST_TOKEN)

import pytest

import auth
import sandbox
from agent import session as agent_session

TOKEN = auth.token()


# ════════════════════════════════════════════════════════════
#                        假的模型返回
# ════════════════════════════════════════════════════════════

class FakeFunction:
    """模拟 OpenAI tool_call.function,只保留 loop 真正会读的两个字段。"""

    def __init__(self, name: str, arguments: str):
        """记下工具名与序列化后的参数。"""
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    """模拟一次 tool_call,id 由调用方指定以便断言 tool 消息的配对。"""

    def __init__(self, call_id: str, name: str, arguments: str):
        """记下调用 id 与被调用的函数。"""
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    """
    模拟 ChatCompletionMessage。
    刻意不实现 model_dump / to_dict,让 loop._msg_to_dict 走手写的序列化分支,
    这样测出来的 history 结构就是真实场景里前端会拿到的那一份。
    """

    def __init__(self, content: str = "", tool_calls=None):
        """构造一条 assistant 消息,tool_calls 为空时置 None。"""
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls or None
        self.refusal = None


def make_tool_call(call_id: str, name: str, **arguments) -> FakeToolCall:
    """构造一次 tool_call,参数按 OpenAI 的约定序列化成 JSON 字符串。"""
    import json
    return FakeToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


class ScriptedModel:
    """
    按剧本依次返回 FakeMessage 的模型桩。
    剧本用完之后一直返回最后一条,避免 max_rounds 用例因为剧本不够长而误判。
    记录每次收到的 messages,供断言历史结构。
    """

    def __init__(self, script):
        """收下剧本,并准备好调用记录。"""
        self.script = list(script)
        self.calls = []
        self.index = 0

    def _next(self, messages):
        """取剧本里的下一条,并记下这一轮实际发出去的 messages。"""
        self.calls.append(list(messages or []))
        if self.index < len(self.script):
            msg = self.script[self.index]
            self.index += 1
            return msg
        return self.script[-1] if self.script else FakeMessage("")

    def __call__(self, messages=None, tools=None, **kwargs):
        """非流式入口的桩。"""
        return self._next(messages)

    def stream(self, messages=None, tools=None, **kwargs):
        """
        流式入口的桩,事件形状与 models.request.request_stream 一致。
        .config.json 里 flow=true 时 loop 走的是这一条路径,不打它就会落到
        no_network 的抛异常桩上,整条流被收敛成 stopped=error。
        """
        msg = self._next(messages)
        if msg.content:
            yield {"type": "content", "text": msg.content}
        yield {"type": "done", "message": msg}


# ════════════════════════════════════════════════════════════
#                        全局隔离夹具
# ════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """
    autouse:把模型层的四个入口换成会抛异常的桩。
    loop 在 import 时就把 request / request_stream 绑成了自己的模块级名字,
    所以两边都要打,只打 models.request 是拦不住 loop 的。
    """
    def blocked(*args, **kwargs):
        """任何真实模型调用都在这里炸掉,而不是偷偷走网络。"""
        raise AssertionError("测试期间禁止真实模型调用,请用 scripted_model 夹具")

    import models.request as model_request_mod
    from agent import loop as agent_loop

    monkeypatch.setattr(model_request_mod, "request", blocked, raising=False)
    monkeypatch.setattr(model_request_mod, "request_stream", blocked, raising=False)
    monkeypatch.setattr(agent_loop, "model_request", blocked, raising=False)
    monkeypatch.setattr(agent_loop, "model_request_stream", blocked, raising=False)
    yield


@pytest.fixture(autouse=True)
def clean_sessions():
    """autouse:每个用例前后清空会话表,history / pending / diff 池都不跨用例残留。"""
    agent_session.store().reset()
    yield
    agent_session.store().reset()


@pytest.fixture
def repo_root() -> Path:
    """仓库根目录,给需要真实安装路径的用例(受保护文件、导入顺序回归)用。"""
    return REPO_ROOT


# ════════════════════════════════════════════════════════════
#                      临时工作区与沙箱
# ════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_workspace(tmp_path):
    """
    临时工作区。把 sandbox 的根目录整个替换成它,退出时还原。
    在这个夹具生效期间,任何指向仓库或用户主目录的写入都会被 sandbox 拒掉,
    所以用例即使写错路径也不可能污染仓库。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    original = list(sandbox._ROOTS)
    sandbox.set_roots([str(ws.resolve())])
    try:
        yield ws.resolve()
    finally:
        sandbox._ROOTS = original


@pytest.fixture
def install_scope():
    """
    把根目录换成仓库安装目录,用于验证受保护文件的拒写行为。
    resolve() 只做路径判断不碰磁盘,受保护用例在抛异常时就返回了,不会写到任何文件。
    """
    original = list(sandbox._ROOTS)
    sandbox.set_roots([str(REPO_ROOT)])
    try:
        yield REPO_ROOT
    finally:
        sandbox._ROOTS = original


# ════════════════════════════════════════════════════════════
#                        Flask 应用与客户端
# ════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def app():
    """CodeForge 的 Flask 应用。main 是模块级单例,整个测试会话共用一份。"""
    import main as main_module
    main_module.app.config.update(TESTING=True)
    return main_module.app


@pytest.fixture
def client(app):
    """裸客户端,不带任何认证头,用于验证 401 / 403 分支。"""
    return app.test_client()


@pytest.fixture
def authed_client(app):
    """带 token 与固定 sid 的客户端,业务路由用例都用它。"""
    c = app.test_client()
    c.environ_base["HTTP_" + auth.TOKEN_HEADER.upper().replace("-", "_")] = TOKEN
    c.environ_base["HTTP_" + auth.SESSION_HEADER.upper().replace("-", "_")] = "pytest"
    return c


@pytest.fixture
def http_session():
    """authed_client 对应的那个会话对象,用来直接检查服务端状态。"""
    return agent_session.get("pytest")


# ════════════════════════════════════════════════════════════
#                        模型桩夹具
# ════════════════════════════════════════════════════════════

@pytest.fixture
def scripted_model(monkeypatch):
    """
    返回一个安装器:传入 FakeMessage 剧本,把 loop 的模型入口换成按剧本走的桩。
    非流式与流式两个入口都要打,否则 flow=true 的配置下会漏到 no_network 的桩上。
    返回 ScriptedModel 实例,用例可以从 .calls 里取每一轮实际发出去的 messages。
    """
    def install(script):
        """按剧本安装模型桩并返回它。"""
        from agent import loop as agent_loop
        stub = ScriptedModel(script)
        monkeypatch.setattr(agent_loop, "model_request", stub)
        monkeypatch.setattr(agent_loop, "model_request_stream", stub.stream)
        return stub

    return install
