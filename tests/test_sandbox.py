"""
sandbox.py 的路径校验测试。

沙箱是所有文件操作的唯一入口,它一旦漏掉,后面所有的原子写、审批、diff 都失去意义。
另外带一条导入顺序的回归:模块级 _ROOTS 曾经在 _is_under 定义之前就调用
_default_roots(),导致 import sandbox 无条件抛 NameError,整个应用起不来。
"""
import os
import subprocess
import sys

import pytest

import sandbox


# ════════════════════════════════════════════════════════════
#                        模块可导入性
# ════════════════════════════════════════════════════════════

def test_module_imports_in_a_fresh_interpreter(repo_root):
    """
    回归:全新解释器里 import sandbox 必须成功。
    模块级语句的定义顺序错了只会在冷启动时暴露,已经 import 过的进程里测不出来,
    所以这里必须开子进程。
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import sandbox; print(len(sandbox.get_roots()))"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout.strip()) >= 1


def test_roots_are_absolute():
    """根目录必须全部是绝对路径,否则越界判断会随进程 cwd 漂移。"""
    roots = sandbox.get_roots()
    assert roots
    assert all(os.path.isabs(r) for r in roots)


# ════════════════════════════════════════════════════════════
#                        越界路径
# ════════════════════════════════════════════════════════════

def test_path_outside_root_is_refused(tmp_workspace):
    """根目录之外的路径必须被拒。"""
    outside = os.path.dirname(str(tmp_workspace))
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve(os.path.join(outside, "escape.txt"))


def test_dotdot_escape_is_refused(tmp_workspace):
    """用 .. 往上跳出根目录必须被拒。"""
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve(str(tmp_workspace / ".." / ".." / "escape.txt"))


def test_dotdot_inside_root_is_allowed(tmp_workspace):
    """.. 只要归一化之后仍落在根内就应当放行。"""
    (tmp_workspace / "sub").mkdir()
    resolved = sandbox.resolve(str(tmp_workspace / "sub" / ".." / "ok.txt"))
    assert resolved == str(tmp_workspace / "ok.txt")


def test_path_inside_root_is_allowed(tmp_workspace):
    """根目录内的路径正常返回绝对路径。"""
    assert sandbox.resolve(str(tmp_workspace / "a.txt")) == str(tmp_workspace / "a.txt")


def test_root_itself_is_allowed(tmp_workspace):
    """根目录本身也算在范围内。"""
    assert sandbox.resolve(str(tmp_workspace)) == str(tmp_workspace)


def test_empty_path_is_refused(tmp_workspace):
    """空路径与纯空白路径都必须被拒。"""
    for bad in ("", "   ", None):
        with pytest.raises(sandbox.SandboxError):
            sandbox.resolve(bad)


def test_write_checks_parent_directory(tmp_workspace):
    """
    write=True 时父目录也要在根内。
    否则可以用一个还不存在的路径把文件写到根外面去。
    """
    outside = os.path.dirname(str(tmp_workspace))
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve(os.path.join(outside, "new", "f.txt"), write=True)


# ════════════════════════════════════════════════════════════
#                          符号链接
# ════════════════════════════════════════════════════════════

def _make_symlink(link, target):
    """建符号链接,Windows 上没有权限时跳过整条用例。"""
    try:
        os.symlink(str(target), str(link), target_is_directory=os.path.isdir(str(target)))
    except (OSError, NotImplementedError, AttributeError) as e:
        pytest.skip(f"当前环境无法创建符号链接: {e}")


def test_symlink_cannot_escape_root(tmp_workspace, tmp_path):
    """
    根内的符号链接指向根外时必须被拒。
    resolve() 跟随符号链接,判断的是链接指向的真实路径而不是链接本身。
    """
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")

    link = tmp_workspace / "escape"
    _make_symlink(link, outside_dir)

    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve(str(link / "secret.txt"))


def test_symlink_file_cannot_escape_root(tmp_workspace, tmp_path):
    """指向根外单个文件的符号链接同样必须被拒。"""
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    link = tmp_workspace / "leak.txt"
    _make_symlink(link, secret)

    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve(str(link))


def test_symlink_inside_root_is_allowed(tmp_workspace):
    """根内指向根内的符号链接应当放行。"""
    real = tmp_workspace / "real"
    real.mkdir()
    (real / "f.txt").write_text("ok", encoding="utf-8")

    link = tmp_workspace / "link"
    _make_symlink(link, real)

    assert sandbox.resolve(str(link / "f.txt")) == str(real / "f.txt")


# ════════════════════════════════════════════════════════════
#                          safe_join
# ════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", ["a/b", "a\\b", "..", ".", "../x", "/abs", "\\abs"])
def test_safe_join_refuses_separators_and_dotdot(tmp_workspace, name):
    """名称里带路径分隔符或 .. 一律拒绝,避免拼出根外路径。"""
    with pytest.raises(sandbox.SandboxError):
        sandbox.safe_join(str(tmp_workspace), name)


@pytest.mark.parametrize("name", ["", " ", " lead", "trail ", "\tx"])
def test_safe_join_refuses_blank_or_padded_names(tmp_workspace, name):
    """空名称、首尾带空白的名称一律拒绝。"""
    with pytest.raises(sandbox.SandboxError):
        sandbox.safe_join(str(tmp_workspace), name)


@pytest.mark.skipif(sys.platform != "win32", reason="盘符语义只在 Windows 上有意义")
def test_safe_join_refuses_drive_letter(tmp_workspace):
    """Windows 上名称里带冒号可能被解释成盘符或数据流,必须拒绝。"""
    with pytest.raises(sandbox.SandboxError):
        sandbox.safe_join(str(tmp_workspace), "C:evil.txt")


def test_safe_join_accepts_plain_name(tmp_workspace):
    """普通文件名正常拼接。"""
    assert sandbox.safe_join(str(tmp_workspace), "ok.txt") == str(tmp_workspace / "ok.txt")


def test_safe_join_refuses_parent_outside_root(tmp_workspace):
    """父目录本身越界时,即使名称合法也要拒绝。"""
    outside = os.path.dirname(str(tmp_workspace))
    with pytest.raises(sandbox.SandboxError):
        sandbox.safe_join(outside, "ok.txt")


# ════════════════════════════════════════════════════════════
#                        受保护的治理文件
# ════════════════════════════════════════════════════════════

PROTECTED = [
    ("agent", "prompt.json"),
    ("models", "model.json"),
    (".config.json",),
    ("sandbox.py",),
]


@pytest.mark.parametrize("parts", PROTECTED, ids=lambda p: "/".join(p))
def test_agent_cannot_write_protected_files(install_scope, parts):
    """agent 写入治理文件必须被拒:改了它们等于改 agent 自己的行为。"""
    target = install_scope.joinpath(*parts)
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve(str(target), write=True, agent=True)


@pytest.mark.parametrize("parts", PROTECTED, ids=lambda p: "/".join(p))
def test_protected_files_are_readable(install_scope, parts):
    """受保护只针对写入,读取不受影响。"""
    target = install_scope.joinpath(*parts)
    assert sandbox.resolve(str(target)) == str(target)


@pytest.mark.parametrize("parts", PROTECTED, ids=lambda p: "/".join(p))
def test_user_can_still_write_protected_files(install_scope, parts):
    """用户在编辑器里手动改治理文件是允许的,拒的只是 agent。"""
    target = install_scope.joinpath(*parts)
    assert sandbox.resolve(str(target), write=True, agent=False) == str(target)


@pytest.mark.parametrize("parts", PROTECTED, ids=lambda p: "/".join(p))
def test_is_protected_reports_true(install_scope, parts):
    """is_protected 对四个治理文件都应当返回 True。"""
    assert sandbox.is_protected(str(install_scope.joinpath(*parts))) is True


def test_is_protected_is_false_for_normal_file(tmp_workspace):
    """普通文件不是受保护文件。"""
    assert sandbox.is_protected(str(tmp_workspace / "a.txt")) is False


def test_is_protected_does_not_raise_on_bad_path():
    """路径非法时 is_protected 返回 False 而不是抛异常。"""
    assert sandbox.is_protected("") is False


def test_protected_check_survives_path_tricks(install_scope):
    """
    受保护判定基于归一化后的真实路径,不能被 ./ 和 .. 之类的写法绕过。
    """
    tricky = install_scope / "agent" / ".." / "agent" / "prompt.json"
    with pytest.raises(sandbox.SandboxError):
        sandbox.resolve(str(tricky), write=True, agent=True)


# ════════════════════════════════════════════════════════════
#                          set_roots
# ════════════════════════════════════════════════════════════

def test_set_roots_ignores_empty_list(tmp_workspace):
    """传空列表不应当把根目录清空,否则会变成全盘放行或全盘拒绝。"""
    before = sandbox.get_roots()
    sandbox.set_roots([])
    assert sandbox.get_roots() == before
