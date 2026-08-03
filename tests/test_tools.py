"""
agent/tool.py 的审批门与结果契约测试。

审批门是 agent 唯一的刹车:它一旦放行错了,模型就能在用户没点头的情况下写文件、
跑命令。这里逐条验证「什么时候必须停下」,以及失败时 ToolResult.ok 确实是 False。
"""
import json

import pytest

from agent import session as agent_session
from agent import tool
from agent.tool import ToolContext, ToolResult


# ════════════════════════════════════════════════════════════
#                          辅助
# ════════════════════════════════════════════════════════════

@pytest.fixture
def sess():
    """一个干净的会话,auto 默认 False(需要逐步确认)。"""
    return agent_session.get("tools-test")


def ctx_for(sess, auto: bool = False) -> ToolContext:
    """按指定的 auto 值构造执行上下文。"""
    sess.auto = auto
    return ToolContext(session=sess, auto=auto)


def approve(sess, action: str, args: dict) -> None:
    """模拟用户在前端点了确认:把 pending 转成一次性授权。"""
    sess.set_pending({"action": action, "args": args})
    assert sess.approve_pending() is not None


# ════════════════════════════════════════════════════════════
#                       run_command 的确认
# ════════════════════════════════════════════════════════════

def test_run_command_requires_confirmation_when_auto_off(sess, tmp_workspace):
    """auto=False 时 run_command 必须停下等确认。"""
    result = tool.dispatch(
        "run_command", {"command": "echo hi", "cwd": str(tmp_workspace)}, ctx_for(sess, auto=False)
    )
    assert result.pending is not None
    assert result.pending["action"] == "run_command"
    assert "pending_approval" in result.content


def test_run_command_requires_confirmation_even_when_auto_on(sess, tmp_workspace):
    """
    always_confirm 的语义:auto=True 也不放行。
    run_command 能执行任意命令,不能因为用户打开了自动模式就变成无人值守。
    """
    result = tool.dispatch(
        "run_command", {"command": "echo hi", "cwd": str(tmp_workspace)}, ctx_for(sess, auto=True)
    )
    assert result.pending is not None
    assert result.pending["action"] == "run_command"


def test_run_command_spec_declares_always_confirm():
    """策略写在注册表里而不是散落在各个工具函数里。"""
    spec = tool.get_spec("run_command")
    assert spec.mutating is True
    assert spec.always_confirm is True
    assert spec.risk == "high"
    assert spec.approval_keys == ("command", "cwd")


def test_run_command_executes_after_approval(sess, tmp_workspace):
    """拿到针对本次调用的一次性授权之后才真正执行。"""
    args = {"command": "echo codeforge", "cwd": str(tmp_workspace)}
    approve(sess, "run_command", args)

    result = tool.dispatch("run_command", args, ctx_for(sess, auto=False))

    assert result.pending is None
    assert "codeforge" in result.content


# ════════════════════════════════════════════════════════════
#                       一次性授权只放行一次
# ════════════════════════════════════════════════════════════

def test_approval_is_consumed_once(sess, tmp_workspace):
    """同一份授权第二次调用必须重新停下,不能变成长期通行证。"""
    args = {"command": "echo once", "cwd": str(tmp_workspace)}
    approve(sess, "run_command", args)

    first = tool.dispatch("run_command", args, ctx_for(sess, auto=False))
    second = tool.dispatch("run_command", args, ctx_for(sess, auto=False))

    assert first.pending is None
    assert second.pending is not None


def test_approval_for_command_a_does_not_release_command_b(sess, tmp_workspace):
    """
    确认了命令 A 不能被拿去执行命令 B。
    approval_keys 里的参数要逐字比对,否则模型换个 command 就绕过了确认。
    """
    approve(sess, "run_command", {"command": "echo safe", "cwd": str(tmp_workspace)})

    result = tool.dispatch(
        "run_command",
        {"command": "echo evil", "cwd": str(tmp_workspace)},
        ctx_for(sess, auto=False),
    )

    assert result.pending is not None
    assert "echo evil" in result.pending["markdown"]


def test_approval_for_one_file_does_not_release_another(sess, tmp_workspace):
    """确认了文件 A 的写入,不能被拿去写文件 B。"""
    approve(sess, "create_file", {"file_path": str(tmp_workspace / "a.txt")})

    result = tool.dispatch(
        "create_file",
        {"file_path": str(tmp_workspace / "b.txt"), "content": "x"},
        ctx_for(sess, auto=False),
    )

    assert result.pending is not None
    assert not (tmp_workspace / "b.txt").exists()


def test_approval_for_one_tool_does_not_release_another(sess, tmp_workspace):
    """确认了 create_file 不能放行 remove_file。"""
    target = tmp_workspace / "keep.txt"
    target.write_text("keep", encoding="utf-8")
    approve(sess, "create_file", {"file_path": str(target)})

    result = tool.dispatch("remove_file", {"file_path": str(target)}, ctx_for(sess, auto=False))

    assert result.pending is not None
    assert target.exists()


# ════════════════════════════════════════════════════════════
#                    mutating 工具的 pending
# ════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", ["create_file", "edit_file", "remove_file"])
def test_file_tools_pend_when_auto_off(sess, tmp_workspace, name):
    """auto=False 时三个文件工具都必须返回 pending 且不碰磁盘。"""
    target = tmp_workspace / "pending.txt"
    target.write_text("original", encoding="utf-8")
    args = {"file_path": str(target)}
    if name == "create_file":
        args = {"file_path": str(tmp_workspace / "new.txt"), "content": "x"}
    elif name == "edit_file":
        args["patches"] = [{"old": "original", "new": "changed"}]

    result = tool.dispatch(name, args, ctx_for(sess, auto=False))

    assert result.pending is not None
    assert result.pending["action"] == name
    assert json.loads(result.content)["status"] == "pending_approval"
    assert target.read_text(encoding="utf-8") == "original"
    assert not (tmp_workspace / "new.txt").exists()


def test_pending_is_written_to_the_session(sess, tmp_workspace):
    """pending 要挂到会话上,前端才能取出来渲染确认卡片。"""
    tool.dispatch(
        "create_file",
        {"file_path": str(tmp_workspace / "x.txt"), "content": ""},
        ctx_for(sess, auto=False),
    )
    assert sess.pending is not None
    assert sess.pending["action"] == "create_file"


def test_file_tools_run_when_auto_on(sess, tmp_workspace):
    """auto=True 时非 always_confirm 的文件工具直接执行。"""
    target = tmp_workspace / "auto.txt"
    result = tool.dispatch(
        "create_file", {"file_path": str(target), "content": "hi"}, ctx_for(sess, auto=True)
    )
    assert result.ok is True
    assert result.pending is None
    assert target.read_text(encoding="utf-8") == "hi"


@pytest.mark.parametrize("name", ["list_dir", "read_file"])
def test_read_tools_never_pend(sess, tmp_workspace, name):
    """只读工具不是 mutating,任何时候都不应当停下。"""
    target = tmp_workspace / "readable.txt"
    target.write_text("content\n", encoding="utf-8")
    args = {"path": str(tmp_workspace)} if name == "list_dir" else {"path": str(target)}

    result = tool.dispatch(name, args, ctx_for(sess, auto=False))

    assert result.pending is None
    assert result.ok is True


# ════════════════════════════════════════════════════════════
#                     失败时 ok 必须是 False
# ════════════════════════════════════════════════════════════

def test_unknown_tool_returns_not_ok(sess):
    """未知工具名。"""
    result = tool.dispatch("no_such_tool", {}, ctx_for(sess, auto=True))
    assert result.ok is False
    assert result.error == "unknown_tool"
    assert isinstance(result, ToolResult)


def test_non_dict_arguments_return_not_ok(sess):
    """参数不是 JSON 对象。"""
    result = tool.dispatch("list_dir", ["not", "a", "dict"], ctx_for(sess, auto=True))
    assert result.ok is False
    assert result.error == "bad_arguments"


def test_unexpected_keyword_returns_not_ok(sess, tmp_workspace):
    """参数名对不上签名时收敛成 bad_arguments,而不是抛 TypeError。"""
    result = tool.dispatch(
        "list_dir", {"path": str(tmp_workspace), "bogus": 1}, ctx_for(sess, auto=True)
    )
    assert result.ok is False
    assert result.error == "bad_arguments"


def test_patch_not_matched_returns_not_ok(sess, tmp_workspace):
    """edit_file 的 old 匹配不上时必须报失败,不能假装成功。"""
    target = tmp_workspace / "nomatch.py"
    target.write_text("print(1)\n", encoding="utf-8")

    result = tool.dispatch(
        "edit_file",
        {"file_path": str(target), "patches": [{"old": "不存在", "new": "x"}]},
        ctx_for(sess, auto=True),
    )

    assert result.ok is False
    assert result.error == "patch_failed"


def test_patch_matching_multiple_places_returns_not_ok(sess, tmp_workspace):
    """old 匹配到多处时要求更精确的上下文,不能随便替换第一处。"""
    target = tmp_workspace / "ambiguous.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")

    result = tool.dispatch(
        "edit_file",
        {"file_path": str(target), "patches": [{"old": "x = 1", "new": "x = 2"}]},
        ctx_for(sess, auto=True),
    )

    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_empty_patches_returns_not_ok(sess, tmp_workspace):
    """patches 必须是非空数组。"""
    target = tmp_workspace / "empty.py"
    target.write_text("a\n", encoding="utf-8")
    result = tool.dispatch(
        "edit_file", {"file_path": str(target), "patches": []}, ctx_for(sess, auto=True)
    )
    assert result.ok is False
    assert result.error == "bad_arguments"


def test_create_existing_file_returns_not_ok(sess, tmp_workspace):
    """create_file 禁止覆盖已有文件,避免静默丢内容。"""
    target = tmp_workspace / "exists.txt"
    target.write_text("original", encoding="utf-8")

    result = tool.dispatch(
        "create_file", {"file_path": str(target), "content": "new"}, ctx_for(sess, auto=True)
    )

    assert result.ok is False
    assert result.error == "file_exists"
    assert target.read_text(encoding="utf-8") == "original"


def test_remove_missing_file_returns_not_ok(sess, tmp_workspace):
    """删除不存在的文件应当收敛成失败结果。"""
    result = tool.dispatch(
        "remove_file", {"file_path": str(tmp_workspace / "ghost.txt")}, ctx_for(sess, auto=True)
    )
    assert result.ok is False
    assert result.retryable is False


def test_out_of_sandbox_path_returns_sandbox_error(sess, tmp_workspace):
    """越界路径统一收敛成 error='sandbox' 而不是把异常抛给 loop。"""
    import os
    outside = os.path.join(os.path.dirname(str(tmp_workspace)), "escape.txt")

    result = tool.dispatch(
        "create_file", {"file_path": outside, "content": "x"}, ctx_for(sess, auto=True)
    )

    assert result.ok is False
    assert result.error == "sandbox"
    assert not os.path.exists(outside)


def test_protected_file_write_is_refused(sess, install_scope):
    """agent 写治理文件必须被拒。"""
    result = tool.dispatch(
        "edit_file",
        {
            "file_path": str(install_scope / "agent" / "prompt.json"),
            "patches": [{"old": "a", "new": "b"}],
        },
        ctx_for(sess, auto=True),
    )
    assert result.ok is False
    assert result.error == "sandbox"


# ════════════════════════════════════════════════════════════
#                        工具清单
# ════════════════════════════════════════════════════════════

def test_plan_tool_is_hidden_when_plan_model_off():
    """plan_model=False 时模型看不到 plan 工具。"""
    with_plan = {t["function"]["name"] for t in tool.get_tools(plan_model=True)}
    without_plan = {t["function"]["name"] for t in tool.get_tools(plan_model=False)}

    assert "plan" in with_plan
    assert "plan" not in without_plan
    assert without_plan < with_plan


def test_registered_tools_cover_the_expected_set():
    """注册表里应当有这七个工具。"""
    assert set(tool.tool_names()) == {
        "plan", "list_dir", "read_file",
        "create_file", "edit_file", "remove_file", "run_command",
    }


def test_tool_schemas_are_openai_shaped():
    """每个工具的 schema 必须符合 OpenAI 的 function calling 格式。"""
    for schema in tool.get_tools(plan_model=True):
        assert schema["type"] == "function"
        fn = schema["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        assert fn["parameters"]["type"] == "object"


def test_plan_tool_writes_pending(sess):
    """plan 工具直调应当写 pending 并返回 markdown 卡片。"""
    result = tool.dispatch(
        "plan",
        {
            "intent": "测试", "direction": "加", "basis": "单元测试",
            "affected_files": ["a.py"], "steps": ["1. 测试"], "risk": "low",
        },
        ctx_for(sess, auto=False),
    )

    assert result.ok is True
    assert "方案确认" in result.content
    assert sess.pending["action"] == "plan"
    assert sess.pending["args"]["affected_files"] == ["a.py"]


def test_read_file_truncates_long_files(sess, tmp_workspace):
    """超长文件必须截断并提示如何续读,否则一次读取就能撑爆上下文。"""
    target = tmp_workspace / "long.txt"
    target.write_text("".join(f"line {i}\n" for i in range(tool.MAX_READ_LINES + 500)), encoding="utf-8")

    result = tool.dispatch("read_file", {"path": str(target)}, ctx_for(sess, auto=True))

    assert result.ok is True
    assert "已截断" in result.content
    assert "start_line=" in result.content
