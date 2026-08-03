"""
agent/diff.py 的行为测试。

这里是全项目 bug 最密集的地方,且几乎全是纯函数,所以覆盖优先级最高。
重点回归:apply_patch 曾经把展示用的带行号内容当成文件内容写回磁盘,
把源码整篇变成 "1 | import os",这条用例逐字节比对内容以确保它不再发生。
"""
import os

import pytest

import sandbox
from agent.diff import DiffStore
from explorer import file as explorer_file


# ════════════════════════════════════════════════════════════
#                          辅助
# ════════════════════════════════════════════════════════════

SOURCE = "import os\nprint(1)\n"


def _write(path, text: str, encoding: str = "utf-8", newline: str = "") -> None:
    """绕过 DiffStore 直接落盘,模拟「文件本来就在那里」或「用户在编辑器里改了」。"""
    with open(path, "w", encoding=encoding, newline=newline) as fp:
        fp.write(text)


def _agent_creates(store: DiffStore, path: str, text: str) -> None:
    """复现 agent create_file 工具的完整调用序列。"""
    store.snapshot_before(path)
    explorer_file.create(path, text)
    store.snapshot_after(path, "create")


def _agent_removes(store: DiffStore, path: str) -> None:
    """复现 agent remove_file 工具的完整调用序列。"""
    store.snapshot_before(path)
    explorer_file.remove_file(path)
    store.snapshot_after(path, "remove")


@pytest.fixture
def store():
    """每个用例一个干净的 DiffStore。"""
    return DiffStore()


# ════════════════════════════════════════════════════════════
#            回归:apply_patch 不得把带行号内容写回磁盘
# ════════════════════════════════════════════════════════════

def test_apply_after_create_leaves_bytes_untouched(store, tmp_workspace):
    """create_file 之后 apply_patch,磁盘内容必须与写入内容逐字节相同。"""
    target = tmp_workspace / "regression.py"
    _agent_creates(store, str(target), SOURCE)
    before = target.read_bytes()

    result = store.apply_patch(str(target))

    assert result["ok"] is True
    assert result["applied"] is False
    assert target.read_bytes() == before
    assert target.read_bytes() == SOURCE.encode("utf-8")
    assert b" | " not in target.read_bytes()


def test_apply_after_create_survives_read_first(store, tmp_workspace):
    """先调 file.read(带行号)再 apply,磁盘内容仍然不能被行号污染。"""
    target = tmp_workspace / "numbered.py"
    _agent_creates(store, str(target), SOURCE)

    numbered = explorer_file.read(str(target))
    assert numbered.startswith("1 | import os")

    store.apply_patch(str(target))
    assert target.read_text(encoding="utf-8") == SOURCE


def test_apply_patch_writes_exactly_the_proposed_text(store, tmp_workspace):
    """store_patch 走 apply 时,落盘的必须是 proposed 原文,不带任何装饰。"""
    target = tmp_workspace / "patched.py"
    _write(target, SOURCE)

    assert store.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])["ok"] is True
    result = store.apply_patch(str(target))

    assert result == {"ok": True, "op": "edit", "applied": True}
    assert target.read_text(encoding="utf-8") == "import os\nprint(2)\n"


def test_revert_after_edit_restores_original_bytes(store, tmp_workspace):
    """撤销一次已落盘的编辑,必须还原成原始字节而不是带行号的展示文本。"""
    target = tmp_workspace / "reverted.py"
    _write(target, SOURCE)
    original = target.read_bytes()

    store.snapshot_before(str(target))
    explorer_file.change(str(target), "print(999)\n", mode="edit", position=1, end_line=2)
    store.snapshot_after(str(target), "edit")
    assert target.read_bytes() != original

    assert store.revert(str(target))["ok"] is True
    assert target.read_bytes() == original


# ════════════════════════════════════════════════════════════
#                    baseline_sha 冲突检测
# ════════════════════════════════════════════════════════════

def test_apply_patch_rejects_when_file_changed_externally(store, tmp_workspace):
    """生成 patch 后文件被外部改动,apply 必须拒绝且不覆盖用户的内容。"""
    target = tmp_workspace / "conflict.py"
    _write(target, SOURCE)
    assert store.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])["ok"] is True

    user_text = "import os\nprint(1)\nuser_added = True\n"
    _write(target, user_text)

    result = store.apply_patch(str(target))

    assert result["ok"] is False
    assert result["conflict"] is True
    assert target.read_text(encoding="utf-8") == user_text


def test_apply_patch_survives_mtime_only_change(store, tmp_workspace):
    """只动 mtime 而内容不变不算冲突,判定必须看 sha 而不是 mtime。"""
    target = tmp_workspace / "touched.py"
    _write(target, SOURCE)
    assert store.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])["ok"] is True

    st = os.stat(target)
    os.utime(target, (st.st_atime + 120, st.st_mtime + 120))

    result = store.apply_patch(str(target))
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "import os\nprint(2)\n"


def test_conflicted_record_is_kept_for_retry(store, tmp_workspace):
    """冲突被拒之后记录必须保留,用户改回原内容还能再 apply 一次。"""
    target = tmp_workspace / "retry.py"
    _write(target, SOURCE)
    store.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])

    _write(target, "外部改动\n")
    assert store.apply_patch(str(target))["ok"] is False

    _write(target, SOURCE)
    assert store.apply_patch(str(target))["ok"] is True


def test_apply_patch_without_record(store, tmp_workspace):
    """没有记录的文件 apply 应当返回失败而不是抛异常。"""
    target = tmp_workspace / "unknown.py"
    _write(target, SOURCE)
    result = store.apply_patch(str(target))
    assert result["ok"] is False


# ════════════════════════════════════════════════════════════
#                     revert 的三种 op
# ════════════════════════════════════════════════════════════

def test_revert_create_deletes_the_file(store, tmp_workspace):
    """agent 新建的文件,撤销后应当从磁盘上消失。"""
    target = tmp_workspace / "created.py"
    _agent_creates(store, str(target), SOURCE)
    assert target.exists()

    result = store.revert(str(target))

    assert result == {"ok": True, "op": "create"}
    assert not target.exists()
    assert store.list_pending() == []


def test_revert_edit_restores_baseline(store, tmp_workspace):
    """已落盘的编辑,撤销后内容回到 agent 动手之前。"""
    target = tmp_workspace / "edited.py"
    _write(target, SOURCE)

    store.snapshot_before(str(target))
    explorer_file.change(str(target), "print(2)\n", mode="edit", position=1, end_line=2)
    store.snapshot_after(str(target), "edit")

    result = store.revert(str(target))

    assert result == {"ok": True, "op": "edit"}
    assert target.read_text(encoding="utf-8") == SOURCE


def test_revert_remove_recreates_the_file(store, tmp_workspace):
    """agent 删掉的文件,撤销后应当把原文恢复回来。"""
    target = tmp_workspace / "gone.py"
    _write(target, SOURCE)
    _agent_removes(store, str(target))
    assert not target.exists()

    result = store.revert(str(target))

    assert result == {"ok": True, "op": "remove"}
    assert target.exists()
    assert target.read_text(encoding="utf-8") == SOURCE


def test_revert_staged_patch_does_not_touch_disk(store, tmp_workspace):
    """还没落盘的 patch 被撤销时,磁盘内容本来就是对的,不能被改写。"""
    target = tmp_workspace / "staged.py"
    _write(target, SOURCE)
    store.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])

    assert store.revert(str(target))["ok"] is True
    assert target.read_text(encoding="utf-8") == SOURCE


def test_revert_unknown_path(store, tmp_workspace):
    """没有记录的文件撤销应当返回失败。"""
    assert store.revert(str(tmp_workspace / "nothing.py"))["ok"] is False


# ════════════════════════════════════════════════════════════
#                  remove 必须出现在 list_pending
# ════════════════════════════════════════════════════════════

def test_remove_shows_up_in_list_pending(store, tmp_workspace):
    """agent 删掉的文件必须在审查列表里留下 op=remove 的卡片。"""
    target = tmp_workspace / "deleted.py"
    _write(target, SOURCE)
    _agent_removes(store, str(target))

    files = store.list_pending()

    assert len(files) == 1
    assert files[0]["op"] == "remove"
    assert files[0]["name"] == "deleted.py"
    assert files[0]["staged"] is False
    assert set(files[0]) == {"path", "name", "op", "staged"}


def test_list_pending_covers_three_ops(store, tmp_workspace):
    """create / edit / remove 三种记录必须同时出现在列表里。"""
    created = tmp_workspace / "a.py"
    edited = tmp_workspace / "b.py"
    removed = tmp_workspace / "c.py"
    _write(edited, SOURCE)
    _write(removed, SOURCE)

    _agent_creates(store, str(created), SOURCE)
    store.store_patch(str(edited), [{"old": "print(1)", "new": "print(2)"}])
    _agent_removes(store, str(removed))

    ops = {item["name"]: item["op"] for item in store.list_pending()}
    assert ops == {"a.py": "create", "b.py": "edit", "c.py": "remove"}


def test_get_diff_works_for_removed_file(store, tmp_workspace):
    """被删掉的文件也要能取到 diff,否则前端的恢复入口无从展示。"""
    target = tmp_workspace / "diffable.py"
    _write(target, SOURCE)
    _agent_removes(store, str(target))

    d = store.get_diff(str(target))

    assert d is not None
    assert d["op"] == "remove"
    assert any(ln["type"] == "del" for ln in d["lines"])


# ════════════════════════════════════════════════════════════
#                       实例之间的隔离
# ════════════════════════════════════════════════════════════

def test_stores_are_isolated(tmp_workspace):
    """两个 DiffStore 各管各的记录,一个会话的改动不能出现在另一个会话里。"""
    a, b = DiffStore(), DiffStore()
    target = tmp_workspace / "shared.py"
    _agent_creates(a, str(target), SOURCE)

    assert len(a.list_pending()) == 1
    assert b.list_pending() == []
    assert b.get_diff(str(target)) is None
    assert b.get_baseline(str(target)) is None
    assert b.revert(str(target))["ok"] is False
    assert target.exists()


def test_clear_on_one_store_does_not_affect_the_other(tmp_workspace):
    """清空一个实例不能连带清掉另一个实例的记录。"""
    a, b = DiffStore(), DiffStore()
    target = tmp_workspace / "both.py"
    _write(target, SOURCE)
    a.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])
    b.store_patch(str(target), [{"old": "print(1)", "new": "print(3)"}])

    a.clear()

    assert a.list_pending() == []
    assert len(b.list_pending()) == 1


# ════════════════════════════════════════════════════════════
#                     基线、编码与沙箱
# ════════════════════════════════════════════════════════════

def test_baseline_keeps_the_earliest_content(store, tmp_workspace):
    """同一文件多次改动只保留最早的基线,撤销一定回到 agent 动手之前。"""
    target = tmp_workspace / "twice.py"
    _write(target, SOURCE)

    store.store_patch(str(target), [{"old": "print(1)", "new": "print(2)"}])
    store.apply_patch(str(target))
    store.store_patch(str(target), [{"old": "print(2)", "new": "print(3)"}])
    store.apply_patch(str(target))

    store.snapshot_before(str(target))
    assert target.read_text(encoding="utf-8") == "import os\nprint(3)\n"


def test_apply_patch_preserves_crlf_and_encoding(store, tmp_workspace):
    """apply 必须沿用原文件的编码与主导行尾,不能把 CRLF 文件整篇翻成 LF。"""
    target = tmp_workspace / "crlf.txt"
    _write(target, "第一行\r\n第二行\r\n", encoding="gbk")

    assert store.store_patch(str(target), [{"old": "第二行", "new": "改过了"}])["ok"] is True
    assert store.apply_patch(str(target))["ok"] is True

    raw = target.read_bytes()
    assert raw == "第一行\r\n改过了\r\n".encode("gbk")
    assert b"\r\r\n" not in raw


def test_store_patch_reports_unmatched_old(store, tmp_workspace):
    """old 匹配不上时 store_patch 返回失败而不是抛异常。"""
    target = tmp_workspace / "nomatch.py"
    _write(target, SOURCE)
    result = store.store_patch(str(target), [{"old": "不存在的内容", "new": "x"}])
    assert result["ok"] is False
    assert result["error"]


def test_store_patch_rejects_noop(store, tmp_workspace):
    """patch 没产生任何变化时应当被拒绝,避免生成空 diff 卡片。"""
    target = tmp_workspace / "noop.py"
    _write(target, SOURCE)
    result = store.store_patch(str(target), [{"old": "print(1)", "new": "print(1)"}])
    assert result["ok"] is False


def test_store_patch_rejects_out_of_sandbox(store, tmp_workspace):
    """越界路径在 store_patch 这一层就被收敛成失败返回。"""
    outside = os.path.join(os.path.dirname(str(tmp_workspace)), "outside.py")
    result = store.store_patch(outside, [{"old": "a", "new": "b"}])
    assert result["ok"] is False


def test_lookup_of_out_of_sandbox_path_is_none(store, tmp_workspace):
    """只读查询遇到越界路径应当当作查不到,而不是抛 SandboxError。"""
    outside = os.path.join(os.path.dirname(str(tmp_workspace)), "outside.py")
    assert store.get_diff(outside) is None
    assert store.get_baseline(outside) is None
    assert store.has_pending(outside) is False


def test_protected_file_is_refused(store, install_scope):
    """agent 不得对治理文件生成 patch。"""
    with pytest.raises(sandbox.SandboxError):
        store._key(str(install_scope / "agent" / "prompt.json"))
