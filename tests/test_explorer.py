"""
explorer/file.py 的读写测试。

这一层是所有落盘动作的收口:原子性、编码探测、行尾保持三件事任何一件出错,
用户的文件都会被静默损坏 —— 而且损坏发生在写入的瞬间,事后无法从内容反推。
迁移自旧的 test/test.py 第 5 节,补上了它没覆盖的编码、行尾与原子性。
"""
import os

import pytest

import sandbox
from explorer import file as explorer_file


# ════════════════════════════════════════════════════════════
#                          原子写
# ════════════════════════════════════════════════════════════

def test_failed_write_leaves_original_intact(tmp_workspace, monkeypatch):
    """
    替换过程中失败时,原文件必须原封不动。
    真实故障是磁盘写满或进程被杀,这里用 os.replace 抛异常来等价模拟。
    """
    target = tmp_workspace / "atomic.txt"
    target.write_text("原始内容\n", encoding="utf-8")
    original = target.read_bytes()

    def boom(src, dst):
        """模拟替换阶段失败。"""
        raise OSError("模拟替换失败")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        explorer_file.write_text(str(target), "新内容,不该落盘\n")

    assert target.read_bytes() == original


def test_failed_write_leaves_no_temp_file(tmp_workspace, monkeypatch):
    """写入失败时临时文件必须被清掉,不能在用户目录里留垃圾。"""
    target = tmp_workspace / "leftover.txt"
    target.write_text("原始\n", encoding="utf-8")

    def boom(src, dst):
        """模拟替换阶段失败。"""
        raise OSError("模拟替换失败")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        explorer_file.write_text(str(target), "新内容\n")

    leftovers = [p.name for p in tmp_workspace.iterdir() if p.name.startswith(".codeforge-")]
    assert leftovers == []


def test_successful_write_leaves_no_temp_file(tmp_workspace):
    """正常写入之后目录里只应当有目标文件。"""
    target = tmp_workspace / "clean.txt"
    explorer_file.write_text(str(target), "内容\n")
    assert sorted(p.name for p in tmp_workspace.iterdir()) == ["clean.txt"]


def test_write_text_returns_absolute_path(tmp_workspace):
    """write_text 返回沙箱校验后的绝对路径。"""
    target = tmp_workspace / "abs.txt"
    assert explorer_file.write_text(str(target), "x") == str(target)


def test_write_outside_sandbox_is_refused(tmp_workspace):
    """越界写入必须被拒。"""
    outside = os.path.join(os.path.dirname(str(tmp_workspace)), "escape.txt")
    with pytest.raises(sandbox.SandboxError):
        explorer_file.write_text(outside, "x")
    assert not os.path.exists(outside)


# ════════════════════════════════════════════════════════════
#                        编码探测往返
# ════════════════════════════════════════════════════════════

def test_utf8_roundtrip(tmp_workspace):
    """utf-8 文件读出来编码应当是 utf-8,写回后字节一致。"""
    target = tmp_workspace / "utf8.txt"
    target.write_bytes("中文内容\n".encode("utf-8"))

    text, encoding = explorer_file.read_text_with_encoding(str(target))

    assert text == "中文内容\n"
    assert encoding == "utf-8"
    explorer_file.write_text(str(target), text, encoding=encoding)
    assert target.read_bytes() == "中文内容\n".encode("utf-8")


def test_gbk_roundtrip_does_not_corrupt(tmp_workspace):
    """
    GBK 文件读写往返不得损坏。
    按 utf-8 读会抛 UnicodeDecodeError,按 utf-8 写回会把整篇变成乱码,
    这条用例锁死「读什么编码就写什么编码」。
    """
    target = tmp_workspace / "gbk.txt"
    raw = "编码测试:你好世界\n第二行\n".encode("gbk")
    target.write_bytes(raw)

    text, encoding = explorer_file.read_text_with_encoding(str(target))

    assert text == "编码测试:你好世界\n第二行\n"
    assert encoding.lower().replace("-", "") in ("gbk", "cp936", "gb2312", "gb18030")

    explorer_file.write_text(str(target), text, encoding=encoding)
    assert target.read_bytes() == raw


def test_bom_is_stripped_and_restored(tmp_workspace):
    """带 BOM 的文件:读出来不含 BOM,按 utf-8-sig 写回时 BOM 仍在。"""
    target = tmp_workspace / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbf" + "内容\n".encode("utf-8"))

    text, encoding = explorer_file.read_text_with_encoding(str(target))

    assert text == "内容\n"
    assert not text.startswith("﻿")
    assert encoding == "utf-8-sig"

    explorer_file.write_text(str(target), text, encoding=encoding)
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")


def test_binary_file_is_refused(tmp_workspace):
    """含 NUL 字节的二进制文件拒绝按文本读取。"""
    target = tmp_workspace / "binary.bin"
    target.write_bytes(b"\x89PNG\x00\x1a\n")
    with pytest.raises(ValueError):
        explorer_file.read_text_with_encoding(str(target))


# ════════════════════════════════════════════════════════════
#                          行尾保持
# ════════════════════════════════════════════════════════════

def test_lf_file_stays_lf_on_windows(tmp_workspace):
    """
    LF 文件在 Windows 上写回仍然是 LF。
    open() 默认 newline=None 会把 \\n 翻译成 \\r\\n,整篇文件的行尾会被悄悄改掉。
    """
    target = tmp_workspace / "lf.txt"
    target.write_bytes(b"line1\nline2\nline3\n")

    text, encoding = explorer_file.read_text_with_encoding(str(target))
    assert explorer_file.detect_newline(text) == "\n"

    explorer_file.write_text(str(target), text, encoding=encoding, newline="")
    assert target.read_bytes() == b"line1\nline2\nline3\n"
    assert b"\r" not in target.read_bytes()


def test_crlf_file_stays_crlf(tmp_workspace):
    """CRLF 文件写回仍然是 CRLF,而且不能变成 \\r\\r\\n。"""
    target = tmp_workspace / "crlf.txt"
    target.write_bytes(b"line1\r\nline2\r\n")

    text, encoding = explorer_file.read_text_with_encoding(str(target))
    assert explorer_file.detect_newline(text) == "\r\n"

    explorer_file.write_text(str(target), text, encoding=encoding, newline="")
    assert target.read_bytes() == b"line1\r\nline2\r\n"
    assert b"\r\r\n" not in target.read_bytes()


def test_expand_lf_content_into_crlf(tmp_workspace):
    """把 LF 正文按 newline='\\r\\n' 展开,应当得到干净的 CRLF。"""
    target = tmp_workspace / "expand.txt"
    target.write_bytes(b"a\r\nb\r\n")
    explorer_file.write_text(str(target), "x\ny\n", newline="\r\n")
    assert target.read_bytes() == b"x\r\ny\r\n"


@pytest.mark.parametrize("text,expected", [
    ("a\r\nb\r\n", "\r\n"),
    ("a\nb\n", "\n"),
    ("a\rb\r", "\r"),
    ("没有换行", "\n"),
    ("", "\n"),
    ("a\r\nb\nc\n", "\n"),
    ("a\r\nb\r\nc\n", "\r\n"),
])
def test_detect_newline(text, expected):
    """主导行尾探测:混合行尾时取出现最多的那种。"""
    assert explorer_file.detect_newline(text) == expected


def test_change_preserves_crlf(tmp_workspace):
    """按行编辑之后,原文件的 CRLF 必须保持。"""
    target = tmp_workspace / "edit_crlf.txt"
    target.write_bytes("第一行\r\n第二行\r\n".encode("gbk"))

    explorer_file.change(str(target), "改过的\n", mode="edit", position=1, end_line=2)

    assert target.read_bytes() == "第一行\r\n改过的\r\n".encode("gbk")


def test_append_preserves_encoding_and_newline(tmp_workspace):
    """
    追加写同样要沿用原编码与原行尾。
    正文用长一点的中文:两个汉字的 GBK 字节常常恰好也是合法 UTF-8,
    那种输入任何探测器都无法区分,不适合用来验证「沿用原编码」。
    """
    target = tmp_workspace / "append.txt"
    target.write_bytes("编码测试第一行\r\n".encode("gbk"))

    explorer_file.change(str(target), "编码测试第二行\n", mode="append")

    assert target.read_bytes() == "编码测试第一行\r\n编码测试第二行\r\n".encode("gbk")


def test_short_gbk_is_indistinguishable_from_utf8(tmp_workspace):
    """
    记录探测器的固有边界:短 GBK 文本的字节序列可能同时是合法 UTF-8。
    "头" 的 GBK 编码是 cd b7,正好构成一个合法的双字节 UTF-8 序列,
    按 utf-8 优先的顺序必然判成 utf-8。要真正区分需要统计式探测(会引入新依赖),
    所以这是已知取舍而不是缺陷 —— 这条用例把边界钉住,防止有人误以为它被修好了。
    """
    target = tmp_workspace / "ambiguous.txt"
    target.write_bytes("头".encode("gbk"))

    _, encoding = explorer_file.read_text_with_encoding(str(target))

    assert encoding == "utf-8"


# ════════════════════════════════════════════════════════════
#                          大小上限
# ════════════════════════════════════════════════════════════

def test_oversized_file_is_refused(tmp_workspace):
    """超过 MAX_TEXT_BYTES 的文件拒绝读取,避免一次读爆内存。"""
    target = tmp_workspace / "big.txt"
    target.write_bytes(b"x" * (explorer_file.MAX_TEXT_BYTES + 1))
    with pytest.raises(ValueError):
        explorer_file.read_text_with_encoding(str(target))


def test_file_at_the_limit_is_readable(tmp_workspace):
    """恰好等于上限的文件应当能读,上限是闭区间。"""
    target = tmp_workspace / "exact.txt"
    target.write_bytes(b"x" * explorer_file.MAX_TEXT_BYTES)
    text, _ = explorer_file.read_text_with_encoding(str(target))
    assert len(text) == explorer_file.MAX_TEXT_BYTES


def test_limits_are_sane():
    """上限常量本身要合理,避免被改成 0 之后所有文件都读不了。"""
    assert explorer_file.MAX_TEXT_BYTES == 2 * 1024 * 1024
    assert explorer_file.MAX_LIST_ENTRIES >= 1000


# ════════════════════════════════════════════════════════════
#                          list_dir
# ════════════════════════════════════════════════════════════

def test_list_dir_structure(tmp_workspace):
    """list_dir 的返回结构固定,前端与工具层都按这些键取值。"""
    (tmp_workspace / "sub").mkdir()
    (tmp_workspace / "a.txt").write_text("aaa", encoding="utf-8")

    result = explorer_file.list_dir(str(tmp_workspace))

    assert set(result) == {"path", "folders", "files", "count", "limit", "truncated"}
    assert result["path"] == str(tmp_workspace)
    assert [f["name"] for f in result["folders"]] == ["sub"]
    assert [f["name"] for f in result["files"]] == ["a.txt"]
    assert result["count"] == 2
    assert result["truncated"] is False


def test_list_dir_file_entry_fields(tmp_workspace):
    """文件条目要带 name / path / size / mtime。"""
    (tmp_workspace / "sized.txt").write_bytes(b"12345")
    entry = explorer_file.list_dir(str(tmp_workspace))["files"][0]

    assert set(entry) == {"name", "path", "size", "mtime"}
    assert entry["size"] == 5
    assert entry["mtime"] > 0


def test_list_dir_hides_dotfiles_when_asked(tmp_workspace):
    """show_hidden=False 时不返回 . 开头的条目。"""
    (tmp_workspace / ".hidden").write_text("h", encoding="utf-8")
    (tmp_workspace / "visible.txt").write_text("v", encoding="utf-8")

    shown = explorer_file.list_dir(str(tmp_workspace), show_hidden=False)
    hidden = explorer_file.list_dir(str(tmp_workspace), show_hidden=True)

    assert [f["name"] for f in shown["files"]] == ["visible.txt"]
    assert len(hidden["files"]) == 2


def test_list_dir_truncates_at_max_entries(tmp_workspace):
    """条目数超过上限时截断,并把 truncated 置为 True。"""
    for i in range(10):
        (tmp_workspace / f"f{i}.txt").write_text("x", encoding="utf-8")

    result = explorer_file.list_dir(str(tmp_workspace), max_entries=4)

    assert result["truncated"] is True
    assert result["count"] == 4
    assert len(result["folders"]) + len(result["files"]) == 4


def test_list_dir_sorted_case_insensitively(tmp_workspace):
    """条目按名称排序且忽略大小写,否则前端列表顺序会跳。"""
    for name in ("Beta.txt", "alpha.txt", "Gamma.txt"):
        (tmp_workspace / name).write_text("x", encoding="utf-8")

    names = [f["name"] for f in explorer_file.list_dir(str(tmp_workspace))["files"]]
    assert names == ["alpha.txt", "Beta.txt", "Gamma.txt"]


def test_list_dir_missing_path(tmp_workspace):
    """路径不存在抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        explorer_file.list_dir(str(tmp_workspace / "nope"))


def test_list_dir_on_a_file(tmp_workspace):
    """对文件调 list_dir 抛 NotADirectoryError。"""
    target = tmp_workspace / "notdir.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        explorer_file.list_dir(str(target))


def test_list_dir_outside_sandbox(tmp_workspace):
    """越界目录必须被拒。"""
    with pytest.raises(sandbox.SandboxError):
        explorer_file.list_dir(os.path.dirname(str(tmp_workspace)))


# ════════════════════════════════════════════════════════════
#              基础读写(迁移自 test/test.py 第 5 节)
# ════════════════════════════════════════════════════════════

def test_create_read_change_remove(tmp_workspace):
    """create / change / read / remove_file 的基本闭环。"""
    target = tmp_workspace / "a.txt"

    assert explorer_file.create(str(target)) is None
    assert target.exists()

    explorer_file.change(str(target), "hello\n", mode="append")
    explorer_file.change(str(target), "world\n", mode="append")
    content = explorer_file.read(str(target))

    assert "hello" in content
    assert "world" in content
    assert "1 |" in content and "2 |" in content

    explorer_file.change(str(target), "EDITED\n", mode="edit", position=0, end_line=1)
    assert "EDITED" in explorer_file.read(str(target), start_line=1).split("\n")[0]

    explorer_file.remove_file(str(target))
    assert not target.exists()


def test_read_adds_line_numbers(tmp_workspace):
    """read 给每行加行号,给模型和前端展示用。"""
    target = tmp_workspace / "numbered.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    assert explorer_file.read(str(target)) == "1 | a\n2 | b\n3 | c\n"


def test_read_start_line(tmp_workspace):
    """start_line 是 1-indexed 且包含起始行。"""
    target = tmp_workspace / "slice.txt"
    target.write_text("a\nb\nc\nd\n", encoding="utf-8")
    assert explorer_file.read(str(target), start_line=3) == "3 | c\n4 | d\n"


def test_read_raw_has_no_line_numbers(tmp_workspace):
    """read_raw 返回不带行号的原文,patch 的基线必须用它。"""
    target = tmp_workspace / "raw.txt"
    target.write_text("import os\nprint(1)\n", encoding="utf-8")
    assert explorer_file.read_raw(str(target)) == "import os\nprint(1)\n"


def test_read_raw_normalizes_newlines_by_default(tmp_workspace):
    """
    read_raw 缺省归一成 LF。
    下游 agent/diff.py 用 newline=None 写回,若这里直接吐 CRLF,
    Windows 上会被再翻译一次变成 \\r\\r\\n,把文件写坏。
    """
    target = tmp_workspace / "rawcrlf.txt"
    target.write_bytes(b"a\r\nb\r\n")
    assert explorer_file.read_raw(str(target)) == "a\nb\n"
    assert explorer_file.read_raw(str(target), keep_newline=True) == "a\r\nb\r\n"


def test_create_with_content(tmp_workspace):
    """create 可以直接带初始内容。"""
    target = tmp_workspace / "with_content.txt"
    explorer_file.create(str(target), "初始内容\n")
    assert target.read_text(encoding="utf-8") == "初始内容\n"


def test_change_rejects_unknown_mode(tmp_workspace):
    """未知 mode 抛 ValueError。"""
    target = tmp_workspace / "mode.txt"
    target.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        explorer_file.change(str(target), "y", mode="不存在的模式")


def test_change_edit_requires_position(tmp_workspace):
    """edit 模式缺 position 抛 ValueError。"""
    target = tmp_workspace / "pos.txt"
    target.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        explorer_file.change(str(target), "y", mode="edit")


def test_remove_missing_file(tmp_workspace):
    """删除不存在的文件抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        explorer_file.remove_file(str(tmp_workspace / "ghost.txt"))


def test_remove_directory_is_refused(tmp_workspace):
    """remove_file 不能用来删目录。"""
    (tmp_workspace / "adir").mkdir()
    with pytest.raises((NotADirectoryError, IsADirectoryError, PermissionError, OSError)):
        explorer_file.remove_file(str(tmp_workspace / "adir"))


def test_listdir_names_is_sandboxed(tmp_workspace):
    """listdir_names 是 os.listdir 的沙箱包装,越界必须被拒。"""
    (tmp_workspace / "x.txt").write_text("x", encoding="utf-8")
    assert explorer_file.listdir_names(str(tmp_workspace)) == ["x.txt"]
    with pytest.raises(sandbox.SandboxError):
        explorer_file.listdir_names(os.path.dirname(str(tmp_workspace)))


# ════════════════════════════════════════════════════════════
#                     apply_patches_content
# ════════════════════════════════════════════════════════════

def test_apply_patches_replaces_once():
    """正常替换一处。"""
    out = explorer_file.apply_patches_content("a\nb\nc\n", [{"old": "b", "new": "B"}])
    assert out == "a\nB\nc\n"


def test_apply_patches_applies_in_order():
    """多个 patch 依次作用在前一次的结果上。"""
    out = explorer_file.apply_patches_content(
        "1\n2\n", [{"old": "1", "new": "one"}, {"old": "2", "new": "two"}]
    )
    assert out == "one\ntwo\n"


def test_apply_patches_rejects_missing_old():
    """匹配不到 old 时报错,不能静默跳过。"""
    with pytest.raises(ValueError, match="未找到"):
        explorer_file.apply_patches_content("a\n", [{"old": "z", "new": "x"}])


def test_apply_patches_rejects_ambiguous_old():
    """old 匹配到多处时要求更精确的上下文。"""
    with pytest.raises(ValueError, match="匹配到"):
        explorer_file.apply_patches_content("a\na\n", [{"old": "a", "new": "b"}])


def test_apply_patches_rejects_empty_old():
    """old 为空会命中任意位置,必须拒绝。"""
    with pytest.raises(ValueError):
        explorer_file.apply_patches_content("a\n", [{"old": "", "new": "x"}])


def test_apply_patches_allows_deletion():
    """new 为空串等价于删除,应当允许。"""
    assert explorer_file.apply_patches_content("ab\n", [{"old": "a", "new": ""}]) == "b\n"


# ════════════════════════════════════════════════════════════
#                          文件锁
# ════════════════════════════════════════════════════════════

def test_lock_is_reentrant_in_the_same_thread(tmp_workspace):
    """同一线程重入同一把锁不应当死锁。"""
    target = str(tmp_workspace / "lock.txt")
    with explorer_file.locked(target):
        with explorer_file.locked(target):
            pass


def test_lock_file_is_cleaned_up(tmp_workspace):
    """锁释放后锁文件要删掉,不能留在临时目录里。"""
    target = str(tmp_workspace / "lock2.txt")
    lock_path = explorer_file._lock_path_for(target)
    with explorer_file.locked(target):
        assert os.path.exists(lock_path)
    assert not os.path.exists(lock_path)


def test_lock_files_live_outside_the_workspace(tmp_workspace):
    """锁文件放系统临时目录,不能污染用户的工作区。"""
    target = str(tmp_workspace / "lock3.txt")
    with explorer_file.locked(target):
        assert list(tmp_workspace.iterdir()) == []
