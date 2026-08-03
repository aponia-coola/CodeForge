"""
agent/loop.py 的循环控制测试,模型层全部走桩,不发起任何网络调用。

最要紧的一条:一批 tool_calls 中途触发待确认时,剩下的调用必须补齐占位 tool 消息。
OpenAI 的协议要求每个 tool_call_id 都有配对的 tool 消息,少一条整段 history 就废了,
用户点确认之后续跑会直接被服务端拒绝。
"""
import json

import pytest

from agent import loop as agent_loop
from agent import session as agent_session

from conftest import FakeMessage, make_tool_call


# ════════════════════════════════════════════════════════════
#                          辅助
# ════════════════════════════════════════════════════════════

SID = "loop-test"


@pytest.fixture
def sess():
    """loop 用例共用的会话。"""
    return agent_session.get(SID)


def collect(events):
    """把事件流收集成列表。"""
    return list(events)


def event_names(events) -> list[str]:
    """取事件名序列。"""
    return [e["event"] for e in events]


def done_of(events) -> dict:
    """取 done 事件。"""
    finals = [e for e in events if e["event"] == "done"]
    assert len(finals) == 1, f"done 事件必须恰好一条,实际 {len(finals)}"
    return finals[0]


def tool_call_ids(history) -> list[str]:
    """取 history 中所有 assistant 发起的 tool_call id。"""
    out = []
    for m in history:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                out.append(tc["id"])
    return out


def tool_message_ids(history) -> list[str]:
    """取 history 中所有 tool 消息回应的 id。"""
    return [m["tool_call_id"] for m in history if m.get("role") == "tool"]


# ════════════════════════════════════════════════════════════
#           回归:pending 中途也要补齐 tool 消息
# ════════════════════════════════════════════════════════════

def test_every_tool_call_gets_a_tool_message_when_paused(sess, scripted_model, tmp_workspace):
    """
    一批三个调用,第一个就触发待确认,后两个必须补占位 tool 消息。
    tool_call_id 与 tool 消息严格 1:1,顺序也要一致。
    """
    calls = [
        make_tool_call("call_1", "create_file", file_path=str(tmp_workspace / "a.txt"), content="a"),
        make_tool_call("call_2", "create_file", file_path=str(tmp_workspace / "b.txt"), content="b"),
        make_tool_call("call_3", "list_dir", path=str(tmp_workspace)),
    ]
    scripted_model([FakeMessage(tool_calls=calls)])
    sess.auto = False

    events = collect(agent_loop.run_stream("做三件事", max_rounds=5, sid=SID))
    history = done_of(events)["history"]

    assert tool_call_ids(history) == ["call_1", "call_2", "call_3"]
    assert tool_message_ids(history) == ["call_1", "call_2", "call_3"]


def test_skipped_calls_are_marked_and_not_executed(sess, scripted_model, tmp_workspace):
    """被跳过的调用要打 skipped 标记,而且真的不能落盘。"""
    calls = [
        make_tool_call("call_1", "create_file", file_path=str(tmp_workspace / "first.txt"), content="1"),
        make_tool_call("call_2", "create_file", file_path=str(tmp_workspace / "second.txt"), content="2"),
    ]
    scripted_model([FakeMessage(tool_calls=calls)])
    sess.auto = False

    events = collect(agent_loop.run_stream("两件事", max_rounds=5, sid=SID))
    results = [e for e in events if e["event"] == "tool_result"]

    assert results[0]["skipped"] is False
    assert results[1]["skipped"] is True
    assert results[1]["ok"] is False
    assert not (tmp_workspace / "first.txt").exists()
    assert not (tmp_workspace / "second.txt").exists()


def test_paused_run_reports_pending(sess, scripted_model, tmp_workspace):
    """待确认时 stopped 必须是 pending,并带上 pending 记录。"""
    scripted_model([FakeMessage(tool_calls=[
        make_tool_call("c1", "create_file", file_path=str(tmp_workspace / "x.txt"), content="x"),
    ])])
    sess.auto = False

    events = collect(agent_loop.run_stream("建个文件", max_rounds=5, sid=SID))
    final = done_of(events)

    assert final["stopped"] == "pending"
    assert final["ok"] is True
    assert final["pending"]["action"] == "create_file"
    assert "pending" in event_names(events)


def test_history_is_valid_after_a_full_batch(sess, scripted_model, tmp_workspace):
    """正常跑完一批调用时,配对关系同样要成立。"""
    calls = [
        make_tool_call("a1", "list_dir", path=str(tmp_workspace)),
        make_tool_call("a2", "list_dir", path=str(tmp_workspace)),
    ]
    scripted_model([FakeMessage(tool_calls=calls), FakeMessage(content="做完了")])

    events = collect(agent_loop.run_stream("看两遍", max_rounds=5, sid=SID))
    history = done_of(events)["history"]

    assert tool_call_ids(history) == tool_message_ids(history) == ["a1", "a2"]


def test_bad_json_arguments_still_get_a_tool_message(sess, scripted_model, tmp_workspace):
    """
    参数 JSON 坏掉时也要回一条 tool 消息。
    少回一条 history 就断了,模型下一轮直接收到协议错误。
    """
    from conftest import FakeToolCall

    broken = FakeToolCall("bad_1", "list_dir", "{not json")
    scripted_model([FakeMessage(tool_calls=[broken]), FakeMessage(content="收到")])

    events = collect(agent_loop.run_stream("坏参数", max_rounds=5, sid=SID))
    history = done_of(events)["history"]
    results = [e for e in events if e["event"] == "tool_result"]

    assert tool_message_ids(history) == ["bad_1"]
    assert results[0]["ok"] is False
    assert results[0]["error"] == "bad_arguments"


# ════════════════════════════════════════════════════════════
#                        max_rounds 收敛
# ════════════════════════════════════════════════════════════

def test_loop_stops_at_max_rounds(sess, scripted_model, tmp_workspace):
    """模型一直调工具时,循环必须在 max_rounds 停下而不是无限跑。"""
    stub = scripted_model([FakeMessage(tool_calls=[
        make_tool_call("loop_1", "list_dir", path=str(tmp_workspace)),
    ])])

    events = collect(agent_loop.run_stream("永动机", max_rounds=3, sid=SID))
    final = done_of(events)

    assert final["stopped"] == "max_rounds"
    assert final["ok"] is False
    assert final["rounds"] == 3
    assert len(stub.calls) == 3
    assert event_names(events).count("round") == 3


def test_loop_stops_early_on_answer(sess, scripted_model):
    """模型不再调工具时立刻收尾,不把轮数用满。"""
    stub = scripted_model([FakeMessage(content="直接回答")])

    events = collect(agent_loop.run_stream("问个问题", max_rounds=10, sid=SID))
    final = done_of(events)

    assert final["stopped"] == "answer"
    assert final["ok"] is True
    assert final["answer"] == "直接回答"
    assert final["rounds"] == 1
    assert len(stub.calls) == 1


def test_max_rounds_one(sess, scripted_model, tmp_workspace):
    """max_rounds=1 的边界:跑一轮就停。"""
    scripted_model([FakeMessage(tool_calls=[
        make_tool_call("only", "list_dir", path=str(tmp_workspace)),
    ])])

    final = done_of(collect(agent_loop.run_stream("一轮", max_rounds=1, sid=SID)))
    assert final["stopped"] == "max_rounds"
    assert final["rounds"] == 1


def test_model_failure_is_reported_not_raised(sess, monkeypatch):
    """模型调用抛异常时 loop 要收敛成 stopped=error,不能把异常冒到 HTTP 层。"""
    def boom(**kwargs):
        """模拟模型调用抛异常。"""
        raise ConnectionError("模拟网络故障")

    monkeypatch.setattr(agent_loop, "model_request", boom)

    final = done_of(collect(agent_loop.run_stream("会炸", max_rounds=3, sid=SID)))

    assert final["stopped"] == "error"
    assert final["ok"] is False
    assert "ConnectionError" in final["answer"]


# ════════════════════════════════════════════════════════════
#                        事件序列
# ════════════════════════════════════════════════════════════

def test_event_sequence_for_a_readonly_run(sess, scripted_model, tmp_workspace):
    """只读任务的完整事件序列。"""
    scripted_model([
        FakeMessage(tool_calls=[make_tool_call("r1", "list_dir", path=str(tmp_workspace))]),
        FakeMessage(content="目录是空的"),
    ])

    events = collect(agent_loop.run_stream("看目录", max_rounds=5, sid=SID))

    assert event_names(events) == [
        "start", "round", "tool_call", "tool_result", "round", "done",
    ]


def test_event_sequence_for_a_write_run(sess, scripted_model, tmp_workspace):
    """写文件被批准后,diff_updated 要在 tool_result 之后发出。"""
    scripted_model([
        FakeMessage(tool_calls=[
            make_tool_call("w1", "create_file", file_path=str(tmp_workspace / "w.txt"), content="w"),
        ]),
        FakeMessage(content="写好了"),
    ])
    sess.auto = True

    events = collect(agent_loop.run_stream("写文件", max_rounds=5, sid=SID))

    assert event_names(events) == [
        "start", "round", "tool_call", "tool_result", "diff_updated", "round", "done",
    ]
    assert (tmp_workspace / "w.txt").read_text(encoding="utf-8") == "w"


def test_diff_updated_is_not_emitted_when_paused(sess, scripted_model, tmp_workspace):
    """待确认时什么都没写,不应当发 diff_updated 骗前端刷新。"""
    scripted_model([FakeMessage(tool_calls=[
        make_tool_call("p1", "create_file", file_path=str(tmp_workspace / "p.txt"), content="p"),
    ])])
    sess.auto = False

    events = collect(agent_loop.run_stream("写文件", max_rounds=5, sid=SID))
    assert "diff_updated" not in event_names(events)


def test_start_and_done_carry_sid(sess, scripted_model):
    """start 与 done 都要带 sid,前端才能把事件归到正确的标签页。"""
    scripted_model([FakeMessage(content="ok")])
    events = collect(agent_loop.run_stream("hi", max_rounds=2, sid=SID))

    assert events[0]["event"] == "start"
    assert events[0]["sid"] == SID
    assert events[0]["max_rounds"] == 2
    assert done_of(events)["sid"] == SID


def test_tool_result_event_shape(sess, scripted_model, tmp_workspace):
    """tool_result 事件的字段清单固定,前端按名字取值。"""
    scripted_model([
        FakeMessage(tool_calls=[make_tool_call("s1", "list_dir", path=str(tmp_workspace))]),
        FakeMessage(content="done"),
    ])

    events = collect(agent_loop.run_stream("看", max_rounds=3, sid=SID))
    result = [e for e in events if e["event"] == "tool_result"][0]

    assert set(result) == {"event", "name", "ok", "content", "error", "skipped"}
    assert result["name"] == "list_dir"
    assert result["ok"] is True
    assert result["error"] is None


def test_done_event_shape(sess, scripted_model):
    """done 事件的字段清单固定。"""
    scripted_model([FakeMessage(content="回答")])
    final = done_of(collect(agent_loop.run_stream("问", max_rounds=2, sid=SID)))

    assert set(final) == {
        "event", "answer", "history", "ok", "stopped", "rounds", "tools_used", "pending", "sid",
    }


# ════════════════════════════════════════════════════════════
#                    history 与系统提示词
# ════════════════════════════════════════════════════════════

def test_system_prompt_is_first_and_unique(sess, scripted_model):
    """历史里只能有一条 system 消息,而且必须在最前面。"""
    scripted_model([FakeMessage(content="ok")])
    stale = [
        {"role": "system", "content": "旧的系统提示"},
        {"role": "user", "content": "上一轮"},
        {"role": "assistant", "content": "上一轮回答"},
    ]

    history = done_of(collect(agent_loop.run_stream("新问题", history=stale, max_rounds=2, sid=SID)))["history"]

    assert history[0]["role"] == "system"
    assert sum(1 for m in history if m["role"] == "system") == 1
    assert history[0]["content"] != "旧的系统提示"


def test_cwd_is_injected_into_system_prompt(sess, scripted_model, tmp_workspace):
    """传了 cwd 时系统提示里要带上,否则模型不知道相对路径该拼到哪。"""
    stub = scripted_model([FakeMessage(content="ok")])
    collect(agent_loop.run_stream("问", max_rounds=2, cwd=str(tmp_workspace), sid=SID))

    system = stub.calls[0][0]["content"]
    assert str(tmp_workspace) in system


def test_tools_used_is_deduplicated(sess, scripted_model, tmp_workspace):
    """同一个工具调用多次,tools_used 里只记一次。"""
    scripted_model([
        FakeMessage(tool_calls=[
            make_tool_call("d1", "list_dir", path=str(tmp_workspace)),
            make_tool_call("d2", "list_dir", path=str(tmp_workspace)),
        ]),
        FakeMessage(content="ok"),
    ])

    final = done_of(collect(agent_loop.run_stream("看两次", max_rounds=3, sid=SID)))
    assert final["tools_used"] == ["list_dir"]


def test_run_returns_the_done_event(sess, scripted_model):
    """collect 模式的 run() 应当返回 done 事件的内容。"""
    scripted_model([FakeMessage(content="收尾")])
    out = agent_loop.run("问", max_rounds=2, sid=SID)

    assert out["stopped"] == "answer"
    assert out["answer"] == "收尾"
    assert out["ok"] is True


# ════════════════════════════════════════════════════════════
#                        历史压缩
# ════════════════════════════════════════════════════════════

def _read_round(idx: str, path: str, body: str) -> list:
    """构造一轮 read_file 的 assistant + tool 消息对。"""
    return [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": idx,
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": path})},
            }],
        },
        {"role": "tool", "tool_call_id": idx, "content": body},
    ]


def test_compact_history_replaces_stale_reads():
    """同一路径的旧 read_file 结果被占位符取代,最后一次保留原文。"""
    messages = []
    for i in range(5):
        messages += _read_round(f"r{i}", "/tmp/a.py", f"内容第 {i} 次")

    agent_loop.compact_history(messages, keep_rounds=2)
    bodies = [m["content"] for m in messages if m.get("role") == "tool"]

    assert bodies[0].startswith("[早前读取 ")
    assert bodies[-1] == "内容第 4 次"


def test_compact_history_keeps_recent_rounds():
    """最近 keep_rounds 轮的内容一律原样保留。"""
    messages = []
    for i in range(4):
        messages += _read_round(f"k{i}", f"/tmp/{i}.py", f"内容 {i}")

    agent_loop.compact_history(messages, keep_rounds=2)
    bodies = [m["content"] for m in messages if m.get("role") == "tool"]

    assert bodies[-1] == "内容 3"
    assert bodies[-2] == "内容 2"


def test_compact_history_is_idempotent():
    """重复压缩不应当把占位符再包一层。"""
    messages = []
    for i in range(5):
        messages += _read_round(f"i{i}", "/tmp/a.py", f"内容 {i}")

    agent_loop.compact_history(messages, keep_rounds=2)
    first = [m["content"] for m in messages if m.get("role") == "tool"]
    agent_loop.compact_history(messages, keep_rounds=2)
    second = [m["content"] for m in messages if m.get("role") == "tool"]

    assert first == second


def test_compact_history_ignores_non_read_tools():
    """没有 read_file 的历史不做任何改动。"""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert agent_loop.compact_history(list(messages)) == messages


# ════════════════════════════════════════════════════════════
#                        提示词加载
# ════════════════════════════════════════════════════════════

def test_prompt_info_reports_source_and_hash():
    """prompt_info 要能给出来源与 sha256,便于核对提示词是否被改过。"""
    info = agent_loop.prompt_info()
    assert set(info) == {"path", "sha256", "source", "version"}
    assert info["source"] in ("file", "fallback:read_error", "fallback:parse_error", "fallback:invalid")


def test_prompt_falls_back_when_file_is_broken(monkeypatch, tmp_path):
    """prompt.json 损坏时退回内置副本,不能让整个 agent 起不来。"""
    broken = tmp_path / "prompt.json"
    broken.write_text("{ 不是合法 json", encoding="utf-8")
    monkeypatch.setattr(agent_loop, "_PROMPT_PATH", broken)

    prompt = agent_loop.reload_prompt()
    try:
        assert prompt is agent_loop._FALLBACK_PROMPT
        assert agent_loop.prompt_info()["source"] == "fallback:parse_error"
    finally:
        monkeypatch.undo()
        agent_loop.reload_prompt()
