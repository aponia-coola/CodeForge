"""
agent/session.py 与 agent/state.py 的状态测试。

迁移自旧的 test/test.py 第 1、2 节,并按新的审批语义重写了默认值断言:
auto 现在默认 False(需要逐步确认),确认也不再全局打开 auto,而是写一条
只对被确认的那一个动作放行一次的授权记录。
"""
import pytest

from agent import session as agent_session
from agent import state


SID = "session-test"


@pytest.fixture
def sess():
    """一个干净的会话。"""
    return agent_session.get(SID)


# ════════════════════════════════════════════════════════════
#                          默认值
# ════════════════════════════════════════════════════════════

def test_defaults(sess):
    """新会话的默认值:计划模式开、自动执行关、没有待确认项。"""
    assert sess.plan_model is True
    assert sess.auto is False
    assert sess.pending is None
    assert sess.approved is None
    assert sess.history == []


def test_snapshot_shape(sess):
    """snapshot 的字段清单固定,前端按名字取值。"""
    assert set(sess.snapshot()) == {"sid", "plan_model", "auto", "pending"}
    assert sess.snapshot()["sid"] == SID


def test_each_session_gets_its_own_diff_store(sess):
    """每个会话自带一个 DiffStore,不能是共享的模块级单例。"""
    other = agent_session.get("another")
    assert sess.diffs is not other.diffs


# ════════════════════════════════════════════════════════════
#                          读写与快照
# ════════════════════════════════════════════════════════════

def test_set_and_get(sess):
    """基本的读写往返。"""
    sess.plan_model = False
    sess.auto = True
    sess.set_pending({"action": "test", "args": {}})

    assert sess.plan_model is False
    assert sess.auto is True
    assert sess.pending == {"action": "test", "args": {}}


def test_clear_pending(sess):
    """清空待确认项。"""
    sess.set_pending({"action": "test", "args": {}})
    sess.clear_pending()
    assert sess.pending is None


# ════════════════════════════════════════════════════════════
#                        一次性授权
# ════════════════════════════════════════════════════════════

def test_approve_turns_pending_into_a_one_shot_grant(sess):
    """确认把 pending 转成授权记录并清空 pending,不打开全局 auto。"""
    pending = {"action": "create_file", "args": {"file_path": "/tmp/a.txt"}}
    sess.set_pending(pending)

    approved = sess.approve_pending()

    assert approved == pending
    assert sess.pending is None
    assert sess.approved["action"] == "create_file"
    assert sess.auto is False


def test_approve_without_pending_returns_none(sess):
    """没有待确认项时确认返回 None。"""
    assert sess.approve_pending() is None
    assert sess.approved is None


def test_consume_approval_matches_and_clears(sess):
    """消费一次授权之后它就没了,第二次必须返回 False。"""
    sess.set_pending({"action": "create_file", "args": {"file_path": "/tmp/a.txt"}})
    sess.approve_pending()

    assert sess.consume_approval("create_file", {"file_path": "/tmp/a.txt"}) is True
    assert sess.consume_approval("create_file", {"file_path": "/tmp/a.txt"}) is False


def test_consume_approval_rejects_other_action(sess):
    """授权只对被确认的那个动作生效。"""
    sess.set_pending({"action": "create_file", "args": {"file_path": "/tmp/a.txt"}})
    sess.approve_pending()

    assert sess.consume_approval("remove_file", {"file_path": "/tmp/a.txt"}) is False
    assert sess.approved is not None


def test_consume_approval_rejects_other_file(sess):
    """授权只对被确认的那个文件生效。"""
    sess.set_pending({"action": "create_file", "args": {"file_path": "/tmp/a.txt"}})
    sess.approve_pending()

    assert sess.consume_approval("create_file", {"file_path": "/tmp/b.txt"}) is False


def test_clear_approval(sess):
    """显式清掉授权。"""
    sess.set_pending({"action": "create_file", "args": {}})
    sess.approve_pending()
    sess.clear_approval()
    assert sess.approved is None


# ════════════════════════════════════════════════════════════
#                        会话表隔离
# ════════════════════════════════════════════════════════════

def test_sessions_do_not_share_state():
    """两个 sid 的状态互不影响。"""
    a = agent_session.get("iso-a")
    b = agent_session.get("iso-b")

    a.auto = True
    a.set_pending({"action": "x", "args": {}})

    assert b.auto is False
    assert b.pending is None


def test_same_sid_returns_the_same_object():
    """同一个 sid 拿到的是同一个会话对象。"""
    assert agent_session.get("same") is agent_session.get("same")


def test_blank_sid_falls_back_to_default():
    """空 sid 落到 default 会话。"""
    assert agent_session.get(None).id == agent_session.DEFAULT_SID
    assert agent_session.get("   ").id == agent_session.DEFAULT_SID


def test_store_drop_and_list():
    """会话表的增删查。"""
    agent_session.get("drop-me")
    assert "drop-me" in agent_session.store().list_ids()
    assert agent_session.store().drop("drop-me") is True
    assert "drop-me" not in agent_session.store().list_ids()
    assert agent_session.store().drop("drop-me") is False


# ════════════════════════════════════════════════════════════
#                    state.py 兼容层
# ════════════════════════════════════════════════════════════

def test_state_module_delegates_to_the_session():
    """state 的模块级函数与会话对象读到的是同一份状态。"""
    state.set_plan_model(False, sid=SID)
    state.set_auto(True, sid=SID)
    state.set_pending({"action": "test", "args": {}}, sid=SID)

    sess = agent_session.get(SID)
    assert sess.plan_model is False
    assert sess.auto is True
    assert sess.pending == {"action": "test", "args": {}}

    assert state.get_plan_model(sid=SID) is False
    assert state.get_auto(sid=SID) is True
    assert state.has_pending(sid=SID) is True


def test_state_snapshot_matches_session():
    """两条路径拿到的快照必须一致。"""
    assert state.snapshot(sid=SID) == agent_session.get(SID).snapshot()


def test_state_clear_pending():
    """兼容层的清空同样生效。"""
    state.set_pending({"action": "x", "args": {}}, sid=SID)
    state.clear_pending(sid=SID)
    assert state.get_pending(sid=SID) is None


def test_state_reset_drops_all_sessions():
    """reset 之后所有会话都回到初始状态。"""
    state.set_auto(True, sid=SID)
    state.reset()
    assert agent_session.get(SID).auto is False
