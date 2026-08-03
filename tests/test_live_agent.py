"""
需要真实模型调用的端到端用例,默认不跑。

迁移自旧的 test/test.py 第 8~12 节。那些断言依赖真实的付费 LLM 调用:要联网、
要有效 key,而且模型有自由裁量权,同样的输入不保证走同一条路径,所以它们既慢又不稳,
不适合进常规回归。这里保留下来只是为了偶尔手动验一次真实链路。

跑法:
    .venv\\Scripts\\python.exe -m pytest -m live tests/test_live_agent.py

注意 conftest 的 no_network 夹具是 autouse 的,会把模型入口换成抛异常的桩,
所以每条用例都要显式用 real_model 夹具把它还原回去。
"""
import pytest

from agent import loop as agent_loop
from agent import session as agent_session

pytestmark = pytest.mark.live

SID = "live-test"


@pytest.fixture
def real_model(monkeypatch):
    """把 no_network 打过的桩还原成真实的模型入口。"""
    import models.request as model_request_mod

    monkeypatch.setattr(agent_loop, "model_request", model_request_mod.request.__wrapped__
                        if hasattr(model_request_mod.request, "__wrapped__")
                        else _reimport_request())
    yield


def _reimport_request():
    """重新拿一份未被打桩的 models.request.request。"""
    import importlib
    import models.request
    return importlib.reload(models.request).request


@pytest.fixture
def sess():
    """live 用例共用的会话。"""
    return agent_session.get(SID)


# ════════════════════════════════════════════════════════════
#                        只读任务
# ════════════════════════════════════════════════════════════

def test_readonly_task_converges(real_model, sess, tmp_workspace):
    """模型应当用 list_dir 看目录然后给出自然语言回答。"""
    (tmp_workspace / "a.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_workspace / "b.py").write_text("print(2)\n", encoding="utf-8")

    out = agent_loop.run(
        f"{tmp_workspace} 目录下有哪些 .py 文件?", max_rounds=5, cwd=str(tmp_workspace), sid=SID
    )

    assert out["ok"] is True
    assert out["stopped"] == "answer"
    assert "list_dir" in out["tools_used"]
    assert out["answer"]


# ════════════════════════════════════════════════════════════
#                     写任务与用户确认
# ════════════════════════════════════════════════════════════

def test_write_task_stops_for_plan(real_model, sess, tmp_workspace):
    """写任务应当先调 plan 并停下等确认。"""
    target = tmp_workspace / "created.txt"

    out = agent_loop.run(
        f"创建文件 {target} 写两行 hello", max_rounds=8, cwd=str(tmp_workspace), sid=SID
    )

    assert out["stopped"] == "pending"
    assert out["pending"]["action"] == "plan"
    assert all(k in out["pending"]["args"] for k in
               ("intent", "direction", "basis", "affected_files", "steps", "risk"))
    assert not target.exists()


def test_resume_after_confirmation_creates_the_file(real_model, sess, tmp_workspace):
    """用户确认之后续跑,文件应当真的落地。"""
    target = tmp_workspace / "confirmed.txt"

    first = agent_loop.run(
        f"创建文件 {target} 写一行 hello", max_rounds=8, cwd=str(tmp_workspace), sid=SID
    )
    sess.approve_pending()
    sess.auto = True

    second = agent_loop.run(
        "确认,按计划执行", history=first["history"], max_rounds=8,
        cwd=str(tmp_workspace), sid=SID,
    )

    assert second["stopped"] in ("answer", "pending")
    assert target.exists()
    assert "hello" in target.read_text(encoding="utf-8")
