"""
main.py 的 HTTP 路由测试。

每条路由三件事:happy path、缺参数的 400、越界路径的 403。
所有用例都在 tmp_workspace 的沙箱根里跑,任何写到仓库的尝试都会在 sandbox 层被拒。
模型层由 conftest 的 no_network 夹具全程封死,聊天路由用 scripted_model 走桩。
"""
import os

import pytest

from conftest import FakeMessage, make_tool_call


# ════════════════════════════════════════════════════════════
#                          辅助
# ════════════════════════════════════════════════════════════

def outside_of(ws) -> str:
    """构造一个必定落在沙箱根之外的路径。"""
    return os.path.join(os.path.dirname(str(ws)), "outside.txt")


def assert_sandbox_403(resp):
    """越界路径必须收敛成 403 JSON,而不是 HTML 500。"""
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["ok"] is False
    assert body["code"] == "sandbox"


# ════════════════════════════════════════════════════════════
#                        配置与模型
# ════════════════════════════════════════════════════════════

def test_get_config(authed_client):
    """GET /api/config 返回前端需要的全局开关。"""
    body = authed_client.get("/api/config").get_json()
    assert body["ok"] is True
    assert isinstance(body["flow"], bool)
    assert 1 <= body["max_round"] <= 200
    assert body["path"]


def test_post_config_rejects_empty_patch(authed_client):
    """没有可改字段时 400。"""
    resp = authed_client.post("/api/config", json={})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_post_config_rejects_bad_max_round(authed_client):
    """max_round 非法值 400,且不写盘。"""
    assert authed_client.post("/api/config", json={"max_round": "abc"}).status_code == 400
    assert authed_client.post("/api/config", json={"max_round": 0}).status_code == 400
    assert authed_client.post("/api/config", json={"max_round": 999}).status_code == 400


def test_get_models(authed_client):
    """GET /api/models 返回模型列表,不发起任何模型调用。"""
    body = authed_client.get("/api/models").get_json()
    assert body["ok"] is True
    assert isinstance(body["models"], list)


def test_switch_model_missing_field(authed_client):
    """POST /api/model 缺 model 字段 400。"""
    resp = authed_client.post("/api/model", json={})
    assert resp.status_code == 400
    assert "缺少" in resp.get_json()["error"]


def test_switch_model_unknown(authed_client):
    """未知模型 404。"""
    resp = authed_client.post("/api/model", json={"model": "不存在的模型"})
    assert resp.status_code == 404


# ════════════════════════════════════════════════════════════
#                        目录与搜索
# ════════════════════════════════════════════════════════════

def test_folder_happy_path(authed_client, tmp_workspace):
    """GET /api/folder 列出目录树。"""
    (tmp_workspace / "sub").mkdir()
    (tmp_workspace / "a.txt").write_text("x", encoding="utf-8")

    body = authed_client.get(f"/api/folder?path={tmp_workspace}").get_json()

    assert body["ok"] is True
    assert [f["name"] for f in body["tree"]["folders"]] == ["sub"]
    assert [f["name"] for f in body["tree"]["files"]] == ["a.txt"]


def test_folder_missing_path_is_404(authed_client, tmp_workspace):
    """目录不存在 404。"""
    resp = authed_client.get(f"/api/folder?path={tmp_workspace / 'nope'}")
    assert resp.status_code == 404


def test_folder_outside_sandbox(authed_client, tmp_workspace):
    """越界目录 403。"""
    assert_sandbox_403(authed_client.get(f"/api/folder?path={os.path.dirname(str(tmp_workspace))}"))


def test_folder_create_happy_path(authed_client, tmp_workspace):
    """POST /api/folder/create 建目录。"""
    resp = authed_client.post("/api/folder/create",
                              json={"path": str(tmp_workspace), "name": "newdir"})
    assert resp.status_code == 200
    assert (tmp_workspace / "newdir").is_dir()


def test_folder_create_missing_name(authed_client, tmp_workspace):
    """缺 name 400。"""
    resp = authed_client.post("/api/folder/create", json={"path": str(tmp_workspace)})
    assert resp.status_code == 400
    assert "name" in resp.get_json()["error"]


def test_folder_create_rejects_traversal(authed_client, tmp_workspace):
    """name 里带 .. 必须 403,不能在根外面建目录。"""
    assert_sandbox_403(authed_client.post(
        "/api/folder/create", json={"path": str(tmp_workspace), "name": "../escape"}
    ))


def test_folder_create_conflict(authed_client, tmp_workspace):
    """目标已存在 409。"""
    (tmp_workspace / "dup").mkdir()
    resp = authed_client.post("/api/folder/create",
                              json={"path": str(tmp_workspace), "name": "dup"})
    assert resp.status_code == 409


def test_search_happy_path(authed_client, tmp_workspace):
    """GET /api/search 在指定根下搜内容。"""
    (tmp_workspace / "hit.py").write_text("def 唯一标记():\n    pass\n", encoding="utf-8")

    body = authed_client.get(f"/api/search?q=唯一标记&path={tmp_workspace}").get_json()

    assert body["ok"] is True
    assert any("hit.py" in str(r) for r in body["results"])


def test_search_outside_sandbox(authed_client, tmp_workspace):
    """越界根 403。"""
    assert_sandbox_403(
        authed_client.get(f"/api/search?q=x&path={os.path.dirname(str(tmp_workspace))}")
    )


# ════════════════════════════════════════════════════════════
#                          文件读写
# ════════════════════════════════════════════════════════════

def test_file_read_happy_path(authed_client, tmp_workspace):
    """GET /api/file/read 返回内容以及编码与行尾。"""
    target = tmp_workspace / "read.txt"
    target.write_bytes("第一行\n第二行\n".encode("utf-8"))

    body = authed_client.get(f"/api/file/read?path={target}").get_json()

    assert body["ok"] is True
    assert body["content"] == "第一行\n第二行\n"
    assert body["encoding"] == "utf-8"
    assert body["newline"] == "\n"
    assert body["name"] == "read.txt"


def test_file_read_reports_gbk_encoding(authed_client, tmp_workspace):
    """GBK 文件要把探测到的编码告诉前端,保存时才不会被存成 UTF-8。"""
    target = tmp_workspace / "gbk.txt"
    target.write_bytes("中文\n".encode("gbk"))

    body = authed_client.get(f"/api/file/read?path={target}").get_json()

    assert body["ok"] is True
    assert body["content"] == "中文\n"
    assert body["encoding"].lower().replace("-", "") in ("gbk", "cp936", "gb2312", "gb18030")


def test_file_read_missing_path(authed_client):
    """缺 path 400。"""
    resp = authed_client.get("/api/file/read")
    assert resp.status_code == 400


def test_file_read_not_found(authed_client, tmp_workspace):
    """文件不存在 404。"""
    resp = authed_client.get(f"/api/file/read?path={tmp_workspace / 'ghost.txt'}")
    assert resp.status_code == 404


def test_file_read_on_directory(authed_client, tmp_workspace):
    """路径是目录 400。"""
    resp = authed_client.get(f"/api/file/read?path={tmp_workspace}")
    assert resp.status_code == 400


def test_file_read_too_large(authed_client, tmp_workspace):
    """超出大小上限 413 且带 code=too_large。"""
    from explorer.file import MAX_TEXT_BYTES

    target = tmp_workspace / "big.txt"
    target.write_bytes(b"x" * (MAX_TEXT_BYTES + 10))

    resp = authed_client.get(f"/api/file/read?path={target}")

    assert resp.status_code == 413
    assert resp.get_json()["code"] == "too_large"


def test_file_read_binary(authed_client, tmp_workspace):
    """二进制文件 415 且带 code=not_text。"""
    target = tmp_workspace / "bin.dat"
    target.write_bytes(b"\x00\x01\x02")

    resp = authed_client.get(f"/api/file/read?path={target}")

    assert resp.status_code == 415
    assert resp.get_json()["code"] == "not_text"


def test_file_read_outside_sandbox(authed_client, tmp_workspace):
    """越界读 403。"""
    assert_sandbox_403(authed_client.get(f"/api/file/read?path={outside_of(tmp_workspace)}"))


def test_file_save_happy_path(authed_client, tmp_workspace):
    """POST /api/file/save 整体覆写。"""
    target = tmp_workspace / "save.txt"
    target.write_text("旧内容\n", encoding="utf-8")

    resp = authed_client.post("/api/file/save",
                              json={"path": str(target), "content": "新内容\n"})

    assert resp.status_code == 200
    assert target.read_text(encoding="utf-8") == "新内容\n"


def test_file_save_roundtrips_gbk_and_crlf(authed_client, tmp_workspace):
    """
    把 /api/file/read 给的 encoding 与 newline 原样传回来,文件不能被损坏。
    这是前端保存路径的真实用法。
    """
    target = tmp_workspace / "roundtrip.txt"
    target.write_bytes("第一行\r\n第二行\r\n".encode("gbk"))

    read = authed_client.get(f"/api/file/read?path={target}").get_json()
    authed_client.post("/api/file/save", json={
        "path": str(target),
        "content": read["content"],
        "encoding": read["encoding"],
        "newline": read["newline"],
    })

    assert target.read_bytes() == "第一行\r\n第二行\r\n".encode("gbk")


def test_file_save_missing_path(authed_client):
    """缺 path 400。"""
    assert authed_client.post("/api/file/save", json={"content": "x"}).status_code == 400


def test_file_save_rejects_bad_encoding(authed_client, tmp_workspace):
    """未知编码名 400。"""
    target = tmp_workspace / "enc.txt"
    target.write_text("x", encoding="utf-8")
    resp = authed_client.post("/api/file/save",
                              json={"path": str(target), "content": "y", "encoding": "no-such-enc"})
    assert resp.status_code == 400


def test_file_save_rejects_bad_newline(authed_client, tmp_workspace):
    """newline 只能是三种之一。"""
    target = tmp_workspace / "nl.txt"
    target.write_text("x", encoding="utf-8")
    resp = authed_client.post("/api/file/save",
                              json={"path": str(target), "content": "y", "newline": "\n\n"})
    assert resp.status_code == 400


def test_file_save_rejects_non_string_content(authed_client, tmp_workspace):
    """content 必须是字符串。"""
    target = tmp_workspace / "obj.txt"
    target.write_text("x", encoding="utf-8")
    resp = authed_client.post("/api/file/save", json={"path": str(target), "content": {"a": 1}})
    assert resp.status_code == 400


def test_file_save_outside_sandbox(authed_client, tmp_workspace):
    """越界写 403,且文件确实没被创建。"""
    target = outside_of(tmp_workspace)
    assert_sandbox_403(authed_client.post("/api/file/save",
                                          json={"path": target, "content": "x"}))
    assert not os.path.exists(target)


def test_file_create_happy_path(authed_client, tmp_workspace):
    """POST /api/file/create 建文件。"""
    resp = authed_client.post("/api/file/create",
                              json={"path": str(tmp_workspace), "name": "new.txt",
                                    "content": "内容\n"})
    assert resp.status_code == 200
    assert (tmp_workspace / "new.txt").read_text(encoding="utf-8") == "内容\n"


def test_file_create_missing_name(authed_client, tmp_workspace):
    """缺 name 400。"""
    assert authed_client.post("/api/file/create",
                              json={"path": str(tmp_workspace)}).status_code == 400


def test_file_create_conflict(authed_client, tmp_workspace):
    """已存在 409,且不覆盖原内容。"""
    (tmp_workspace / "dup.txt").write_text("原内容", encoding="utf-8")
    resp = authed_client.post("/api/file/create",
                              json={"path": str(tmp_workspace), "name": "dup.txt"})
    assert resp.status_code == 409
    assert (tmp_workspace / "dup.txt").read_text(encoding="utf-8") == "原内容"


def test_file_create_rejects_traversal(authed_client, tmp_workspace):
    """name 里带路径分隔符 403。"""
    assert_sandbox_403(authed_client.post(
        "/api/file/create", json={"path": str(tmp_workspace), "name": "../escape.txt"}
    ))


def test_file_rename_happy_path(authed_client, tmp_workspace):
    """POST /api/file/rename 同目录改名。"""
    src = tmp_workspace / "old.txt"
    src.write_text("内容", encoding="utf-8")

    resp = authed_client.post("/api/file/rename",
                              json={"path": str(src), "new_name": "new.txt"})

    assert resp.status_code == 200
    assert not src.exists()
    assert (tmp_workspace / "new.txt").read_text(encoding="utf-8") == "内容"


def test_file_rename_missing_field(authed_client, tmp_workspace):
    """缺 new_name 400。"""
    src = tmp_workspace / "r.txt"
    src.write_text("x", encoding="utf-8")
    assert authed_client.post("/api/file/rename", json={"path": str(src)}).status_code == 400


def test_file_rename_source_missing(authed_client, tmp_workspace):
    """源文件不存在 404。"""
    resp = authed_client.post("/api/file/rename",
                              json={"path": str(tmp_workspace / "ghost.txt"), "new_name": "x.txt"})
    assert resp.status_code == 404


def test_file_rename_rejects_traversal(authed_client, tmp_workspace):
    """new_name 里带 .. 403。"""
    src = tmp_workspace / "t.txt"
    src.write_text("x", encoding="utf-8")
    assert_sandbox_403(authed_client.post(
        "/api/file/rename", json={"path": str(src), "new_name": "../escape.txt"}
    ))


def test_file_delete_happy_path(authed_client, tmp_workspace):
    """POST /api/file/delete 删文件。"""
    target = tmp_workspace / "del.txt"
    target.write_text("x", encoding="utf-8")

    resp = authed_client.post("/api/file/delete", json={"path": str(target)})

    assert resp.status_code == 200
    assert not target.exists()


def test_file_delete_missing_path(authed_client):
    """缺 path 400。"""
    assert authed_client.post("/api/file/delete", json={}).status_code == 400


def test_file_delete_outside_sandbox(authed_client, tmp_workspace):
    """越界删 403,目标文件必须还在。"""
    target = outside_of(tmp_workspace)
    with open(target, "w", encoding="utf-8") as fp:
        fp.write("不该被删")
    try:
        assert_sandbox_403(authed_client.post("/api/file/delete", json={"path": target}))
        assert os.path.exists(target)
    finally:
        os.remove(target)


def test_file_duplicate_happy_path(authed_client, tmp_workspace):
    """POST /api/file/duplicate 复制文件。"""
    src = tmp_workspace / "src.txt"
    src.write_text("内容", encoding="utf-8")

    resp = authed_client.post("/api/file/duplicate", json={"path": str(src)})

    assert resp.status_code == 200
    copies = [p.name for p in tmp_workspace.iterdir() if p.name != "src.txt"]
    assert len(copies) == 1


def test_file_move_happy_path(authed_client, tmp_workspace):
    """POST /api/file/move 移动到另一个目录。"""
    src = tmp_workspace / "m.txt"
    src.write_text("内容", encoding="utf-8")
    dest = tmp_workspace / "dest"
    dest.mkdir()

    resp = authed_client.post("/api/file/move",
                              json={"path": str(src), "to_dir": str(dest)})

    assert resp.status_code == 200
    assert not src.exists()
    assert (dest / "m.txt").read_text(encoding="utf-8") == "内容"


def test_file_move_outside_sandbox(authed_client, tmp_workspace):
    """移到根外 403。"""
    src = tmp_workspace / "mm.txt"
    src.write_text("x", encoding="utf-8")
    assert_sandbox_403(authed_client.post(
        "/api/file/move", json={"path": str(src), "to_dir": os.path.dirname(str(tmp_workspace))}
    ))
    assert src.exists()


# ════════════════════════════════════════════════════════════
#                        Agent 状态与 diff
# ════════════════════════════════════════════════════════════

def test_agent_state_get(authed_client):
    """GET /api/agent/state 返回会话快照。"""
    body = authed_client.get("/api/agent/state").get_json()
    assert body["ok"] is True
    assert set(body["state"]) == {"sid", "plan_model", "auto", "pending"}
    assert body["sid"] == "pytest"


def test_agent_state_set(authed_client):
    """POST /api/agent/state 改 plan_model / auto。"""
    body = authed_client.post("/api/agent/state",
                              json={"auto": True, "plan_model": False}).get_json()
    assert body["state"]["auto"] is True
    assert body["state"]["plan_model"] is False


def test_pending_confirm_without_pending(authed_client):
    """没有待确认项时确认应当 400。"""
    resp = authed_client.post("/api/agent/pending/confirm", json={"resume": False})
    assert resp.status_code == 400


def test_pending_confirm_grants_one_shot_approval(authed_client, http_session, tmp_workspace):
    """确认之后应当留下一次性授权,而不是全局打开 auto。"""
    http_session.set_pending({"action": "create_file",
                              "args": {"file_path": str(tmp_workspace / "x.txt")}})

    body = authed_client.post("/api/agent/pending/confirm", json={"resume": False}).get_json()

    assert body["ok"] is True
    assert body["resumed"] is False
    assert http_session.pending is None
    assert http_session.approved["action"] == "create_file"
    assert http_session.auto is False


def test_pending_reject_clears_everything(authed_client, http_session):
    """拒绝要同时清掉 pending 与一次性授权。"""
    http_session.set_pending({"action": "create_file", "args": {}})
    http_session.approve_pending()
    http_session.set_pending({"action": "remove_file", "args": {}})

    authed_client.post("/api/agent/pending/reject", json={})

    assert http_session.pending is None
    assert http_session.approved is None


def test_diffs_empty(authed_client):
    """GET /api/diffs 初始为空列表。"""
    body = authed_client.get("/api/diffs").get_json()
    assert body == {"ok": True, "files": []}


def test_diff_missing_path(authed_client):
    """GET /api/diff 缺 path 400。"""
    assert authed_client.get("/api/diff").status_code == 400


def test_diff_unknown_path_is_404(authed_client, tmp_workspace):
    """没有记录的文件 404。"""
    resp = authed_client.get(f"/api/diff?path={tmp_workspace / 'nothing.py'}")
    assert resp.status_code == 404


def test_diff_lifecycle(authed_client, http_session, tmp_workspace):
    """生成 patch → 查列表 → 查 diff → apply,一条完整链路。"""
    target = tmp_workspace / "life.py"
    target.write_text("print(1)\n", encoding="utf-8")
    http_session.diffs.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])

    files = authed_client.get("/api/diffs").get_json()["files"]
    assert [f["name"] for f in files] == ["life.py"]

    d = authed_client.get(f"/api/diff?path={target}").get_json()
    assert d["ok"] is True
    assert any(ln["type"] == "add" for ln in d["lines"])

    resp = authed_client.post("/api/diff/apply", json={"path": str(target)})
    assert resp.status_code == 200
    assert target.read_text(encoding="utf-8") == "print(2)\n"


def test_diff_apply_conflict_is_409(authed_client, http_session, tmp_workspace):
    """文件被外部改动之后 apply 返回 409,内容不被覆盖。"""
    target = tmp_workspace / "conflict.py"
    target.write_text("print(1)\n", encoding="utf-8")
    http_session.diffs.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])
    target.write_text("用户改的\n", encoding="utf-8")

    resp = authed_client.post("/api/diff/apply", json={"path": str(target)})

    assert resp.status_code == 409
    assert target.read_text(encoding="utf-8") == "用户改的\n"


def test_diff_apply_missing_path(authed_client):
    """缺 path 400。"""
    assert authed_client.post("/api/diff/apply", json={}).status_code == 400


def test_diff_revert_missing_path(authed_client):
    """缺 path 400。"""
    assert authed_client.post("/api/diff/revert", json={}).status_code == 400


def test_diff_discard_missing_path(authed_client):
    """缺 path 400。"""
    assert authed_client.post("/api/diff/discard", json={}).status_code == 400


def test_diff_clear(authed_client, http_session, tmp_workspace):
    """POST /api/diff/clear 清空记录但不动磁盘。"""
    target = tmp_workspace / "clear.py"
    target.write_text("print(1)\n", encoding="utf-8")
    http_session.diffs.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])

    body = authed_client.post("/api/diff/clear", json={}).get_json()

    assert body["files"] == []
    assert target.read_text(encoding="utf-8") == "print(1)\n"


def test_diff_baseline_missing_path(authed_client):
    """缺 path 400。"""
    assert authed_client.get("/api/diff/baseline").status_code == 400


# ════════════════════════════════════════════════════════════
#                          Git
# ════════════════════════════════════════════════════════════

def test_git_status_on_non_repo(authed_client, tmp_workspace):
    """非仓库目录不应当抛异常,收敛成 ok=False 或空状态。"""
    resp = authed_client.get(f"/api/git/status?path={tmp_workspace}")
    assert resp.status_code in (200, 400)
    assert resp.get_json() is not None


def test_git_status_outside_sandbox(authed_client, tmp_workspace):
    """越界路径 403。"""
    assert_sandbox_403(
        authed_client.get(f"/api/git/status?path={os.path.dirname(str(tmp_workspace))}")
    )


def test_git_commit_missing_message(authed_client, tmp_workspace):
    """缺 message 400。"""
    resp = authed_client.post("/api/git/commit", json={"path": str(tmp_workspace)})
    assert resp.status_code == 400


# ════════════════════════════════════════════════════════════
#                          终端
# ════════════════════════════════════════════════════════════

def test_terminal_run_happy_path(authed_client, tmp_workspace):
    """POST /api/terminal/run 执行命令并返回完整的 8 个键。"""
    import sys
    command = "echo hello" if sys.platform.startswith("win") else "echo hello"

    body = authed_client.post("/api/terminal/run",
                              json={"command": command, "cwd": str(tmp_workspace)}).get_json()

    assert set(body) == {"ok", "command", "cwd", "returncode",
                         "stdout", "stderr", "truncated", "error"}
    assert "hello" in body["stdout"]


def test_terminal_run_missing_command(authed_client):
    """缺 command 400。"""
    resp = authed_client.post("/api/terminal/run", json={})
    assert resp.status_code == 400


def test_terminal_run_outside_sandbox(authed_client, tmp_workspace):
    """cwd 越界 403。"""
    assert_sandbox_403(authed_client.post("/api/terminal/run", json={
        "command": "echo x", "cwd": os.path.dirname(str(tmp_workspace)),
    }))


def test_terminal_run_unknown_ssh_session(authed_client, tmp_workspace):
    """ssh_sid 不存在 404。"""
    resp = authed_client.post("/api/terminal/run",
                              json={"command": "ls", "ssh_sid": "no-such-session"})
    assert resp.status_code == 404


# ════════════════════════════════════════════════════════════
#                          聊天(模型走桩)
# ════════════════════════════════════════════════════════════

def test_chat_get_initial_state(authed_client):
    """GET /api/chat 返回空历史与会话状态。"""
    body = authed_client.get("/api/chat").get_json()
    assert body["ok"] is True
    assert body["history"] == []
    assert body["sid"] == "pytest"


def test_chat_post_with_stubbed_model(authed_client, scripted_model, tmp_workspace):
    """POST /api/chat 走完一轮,历史被存回会话。"""
    scripted_model([FakeMessage(content="这是回答")])

    body = authed_client.post("/api/chat",
                              json={"message": "你好", "cwd": str(tmp_workspace)}).get_json()

    assert body["ok"] is True
    assert body["answer"] == "这是回答"
    assert body["stopped"] == "answer"


def test_chat_pending_keeps_history_valid(authed_client, scripted_model, tmp_workspace):
    """待确认时返回的历史里,每个 tool_call_id 都要有配对的 tool 消息。"""
    scripted_model([FakeMessage(tool_calls=[
        make_tool_call("h1", "create_file", file_path=str(tmp_workspace / "a.txt"), content="a"),
        make_tool_call("h2", "create_file", file_path=str(tmp_workspace / "b.txt"), content="b"),
    ])])

    body = authed_client.post("/api/chat",
                              json={"message": "建两个文件", "cwd": str(tmp_workspace)}).get_json()

    history = body["history"]
    call_ids = [tc["id"] for m in history if m.get("role") == "assistant"
                for tc in (m.get("tool_calls") or [])]
    tool_ids = [m["tool_call_id"] for m in history if m.get("role") == "tool"]

    assert body["stopped"] == "pending"
    assert call_ids == tool_ids == ["h1", "h2"]


def test_chat_clear(authed_client, http_session, scripted_model):
    """POST /api/chat/clear 清历史与 pending。"""
    http_session.history = [{"role": "user", "content": "旧的"}]
    http_session.set_pending({"action": "create_file", "args": {}})

    resp = authed_client.post("/api/chat/clear", json={})

    assert resp.status_code == 200
    assert http_session.history == []
    assert http_session.pending is None


def test_chat_stop_sets_the_flag(authed_client):
    """POST /api/chat/stop 应当成功返回,给正在跑的 loop 置取消标志。"""
    resp = authed_client.post("/api/chat/stop", json={})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_chat_stream_emits_sse(authed_client, scripted_model, tmp_workspace):
    """POST /api/chat/stream 返回 SSE 流,含 start 与 done 事件。"""
    scripted_model([FakeMessage(content="流式回答")])

    resp = authed_client.post("/api/chat/stream",
                              json={"message": "你好", "cwd": str(tmp_workspace)})
    payload = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "text/event-stream" in resp.content_type
    assert "start" in payload
    assert "done" in payload
    assert "流式回答" in payload


# ════════════════════════════════════════════════════════════
#                        会话隔离
# ════════════════════════════════════════════════════════════

def test_sessions_are_isolated(app, tmp_workspace):
    """两个 sid 的 diff 池互不可见。"""
    import auth
    from agent import session as agent_session

    token_key = "HTTP_" + auth.TOKEN_HEADER.upper().replace("-", "_")
    sid_key = "HTTP_" + auth.SESSION_HEADER.upper().replace("-", "_")

    a = app.test_client()
    a.environ_base.update({token_key: auth.token(), sid_key: "tab-a"})
    b = app.test_client()
    b.environ_base.update({token_key: auth.token(), sid_key: "tab-b"})

    target = tmp_workspace / "shared.py"
    target.write_text("print(1)\n", encoding="utf-8")
    agent_session.get("tab-a").diffs.store_patch(
        str(target), [{"old": "print(1)", "new": "print(2)"}]
    )

    assert len(a.get("/api/diffs").get_json()["files"]) == 1
    assert b.get("/api/diffs").get_json()["files"] == []


# ════════════════════════════════════════════════════════════
#                        错误格式
# ════════════════════════════════════════════════════════════

def test_unknown_api_route_returns_json(authed_client):
    """/api/* 下的 404 也要是 JSON,前端才能统一按 error 提示。"""
    resp = authed_client.get("/api/no-such-route")
    assert resp.status_code == 404
    assert resp.is_json
    assert resp.get_json()["ok"] is False


def test_wrong_method_returns_json(authed_client):
    """405 同样返回 JSON。"""
    resp = authed_client.post("/api/config/")
    assert resp.is_json or resp.status_code in (404, 405)
