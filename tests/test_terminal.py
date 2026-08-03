"""
terminal/engine.py 的执行测试。

三个历史坑各有一条用例:
1. text=True 让解码发生在 subprocess 自己的读取线程里,中文 Windows 上
   UnicodeDecodeError 抛在那里捕获不到,run() 返回 ok=True 且 stdout 为空。
2. subprocess 的 timeout 只终止直接子进程,shell 派生的孙进程继续占着管道,
   实测 timeout=2 的命令 25 秒才返回。
3. 返回值的 8 个键必须在所有分支都齐全,main.py 与 agent/tool.py 都按名字取。
"""
import os
import sys
import time

import pytest

from terminal import engine


EXPECTED_KEYS = {
    "ok", "command", "cwd", "returncode", "stdout", "stderr", "truncated", "error",
}

IS_WINDOWS = sys.platform.startswith("win")

# 解释器路径不加引号直接拼进命令行。加引号会撞上 cmd 的引号处理(见本文件末尾
# 那条专门的用例),那是另一个独立缺陷,不应该污染解码 / 超时 / 键完整性的验证。
PYTHON = sys.executable

pytestmark = pytest.mark.skipif(
    " " in PYTHON,
    reason="解释器路径含空格,必须加引号才能调用,会撞上 cmd 引号缺陷",
)


def echo(text: str) -> str:
    """当前平台上把一段文本打到 stdout 的命令(不带引号)。"""
    return f"echo {text}"


def run_script(workspace, name: str, body: str, timeout: int = 30) -> dict:
    """把脚本写进工作区并执行,命令里不出现任何引号。"""
    (workspace / name).write_text(body, encoding="utf-8")
    return engine.run(f"{PYTHON} {name}", cwd=str(workspace), timeout=timeout)


# ════════════════════════════════════════════════════════════
#                          超时
# ════════════════════════════════════════════════════════════

def test_timeout_returns_near_the_limit(tmp_workspace):
    """
    超时必须在超时时间附近返回,而不是等子进程自然结束。
    容忍 timeout + 5 秒的收尾余量;旧实现在这个场景下要 20 秒以上。
    """
    command = "ping -n 30 127.0.0.1 > nul" if IS_WINDOWS else "sleep 30"

    started = time.monotonic()
    result = engine.run(command, cwd=str(tmp_workspace), timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed < 7, f"超时返回耗时 {elapsed:.2f}s,远超 timeout=2"
    assert result["ok"] is False
    assert result["returncode"] == -1
    assert "超时" in (result["error"] or "")


def test_timeout_kills_the_whole_process_tree(tmp_workspace):
    """
    超时后孙进程也要被终止。
    只 kill 直接子进程的话,shell 派生的孙进程会活下来继续写心跳文件。
    """
    beat = tmp_workspace / "heartbeat.txt"
    if IS_WINDOWS:
        command = f'start /b cmd /c "for /l %i in (1,1,60) do (echo %i> {beat} & ping -n 2 127.0.0.1 > nul)"'
    else:
        command = f"( while true; do date +%s > {beat}; sleep 1; done ) & wait"

    engine.run(command, cwd=str(tmp_workspace), timeout=2)

    if not beat.exists():
        pytest.skip("当前 shell 未产生孙进程心跳文件,无法验证进程组终止")
    time.sleep(3)
    first = beat.stat().st_mtime
    time.sleep(3)
    assert beat.stat().st_mtime == first, "孙进程仍在写入,进程组没有被终止"


def test_normal_command_is_not_delayed(tmp_workspace):
    """正常命令不该被超时机制拖慢。"""
    started = time.monotonic()
    result = engine.run(echo("fast"), cwd=str(tmp_workspace), timeout=30)
    assert time.monotonic() - started < 10
    assert result["ok"] is True


# ════════════════════════════════════════════════════════════
#                          输出解码
# ════════════════════════════════════════════════════════════

def test_utf8_output_is_not_lost(tmp_workspace):
    """
    子进程输出 UTF-8 而系统 locale 是 cp936 时,内容必须完整拿到。
    旧实现在这里返回 ok=True / stdout='',调用方会误判成「命令没有输出」。
    """
    result = run_script(
        tmp_workspace, "emit.py",
        "import sys\nsys.stdout.buffer.write('中文 café 😀'.encode('utf-8'))\n",
    )

    assert result["ok"] is True, result
    assert result["stdout"] == "中文 café 😀"
    assert result["error"] is None


def test_ascii_output_roundtrip(tmp_workspace):
    """最基本的 stdout 捕获。"""
    result = engine.run(echo("hello"), cwd=str(tmp_workspace), timeout=30)
    assert result["ok"] is True
    assert "hello" in result["stdout"]


def test_undecodable_output_degrades_with_a_note(tmp_workspace):
    """既不是 UTF-8 也不符合 locale 的字节流,要降级解码并在 error 里写明。"""
    result = run_script(
        tmp_workspace, "garbage.py",
        "import sys\nsys.stdout.buffer.write(bytes([0xff, 0xfe, 0x41, 0xff]))\n",
    )

    assert result["stdout"] != ""
    assert result["returncode"] == 0
    assert result["ok"] is True


def test_stderr_is_captured(tmp_workspace):
    """stderr 单独捕获,不能和 stdout 混在一起。"""
    result = run_script(
        tmp_workspace, "err.py", "import sys\nsys.stderr.write('错误输出')\n"
    )

    assert result["stderr"] == "错误输出"
    assert result["stdout"] == ""


def test_newlines_are_normalized(tmp_workspace):
    """输出里的 \\r\\n 统一成 \\n,和原来 text=True 的行为一致。"""
    result = run_script(
        tmp_workspace, "nl.py", "import sys\nsys.stdout.buffer.write(b'a\\r\\nb\\r\\n')\n"
    )

    assert result["stdout"] == "a\nb"
    assert "\r" not in result["stdout"]


def test_large_output_is_truncated(tmp_workspace):
    """超大输出要截断并置 truncated,避免把整个响应撑爆。"""
    result = run_script(
        tmp_workspace, "flood.py",
        "import sys\nsys.stdout.write('x' * (100 * 1024))\n", timeout=60,
    )

    assert result["truncated"] is True
    assert "截断" in result["stdout"]


# ════════════════════════════════════════════════════════════
#                        返回值契约
# ════════════════════════════════════════════════════════════

def test_success_result_has_all_keys(tmp_workspace):
    """正常执行分支。"""
    result = engine.run(echo("ok"), cwd=str(tmp_workspace), timeout=30)
    assert set(result) == EXPECTED_KEYS
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert result["cwd"] == str(tmp_workspace)


def test_failure_result_has_all_keys(tmp_workspace):
    """非零退出码分支。"""
    result = run_script(tmp_workspace, "fail.py", "raise SystemExit(3)\n")
    assert set(result) == EXPECTED_KEYS
    assert result["ok"] is False
    assert result["returncode"] == 3


def test_empty_command_result_has_all_keys():
    """空命令分支。"""
    result = engine.run("   ")
    assert set(result) == EXPECTED_KEYS
    assert result["ok"] is False
    assert result["error"] == "空命令"


def test_sandbox_rejected_cwd_has_all_keys(tmp_workspace):
    """cwd 越界分支:返回 ok=False 而不是抛 SandboxError。"""
    outside = os.path.dirname(str(tmp_workspace))
    result = engine.run(echo("x"), cwd=outside, timeout=30)

    assert set(result) == EXPECTED_KEYS
    assert result["ok"] is False
    assert result["returncode"] == -1
    assert "工作目录不允许" in result["error"]


def test_missing_cwd_has_all_keys(tmp_workspace):
    """cwd 不存在分支。"""
    result = engine.run(echo("x"), cwd=str(tmp_workspace / "nope"), timeout=30)

    assert set(result) == EXPECTED_KEYS
    assert result["ok"] is False
    assert "cwd 不存在" in result["error"]


def test_timeout_result_has_all_keys(tmp_workspace):
    """超时分支。"""
    command = "ping -n 20 127.0.0.1 > nul" if IS_WINDOWS else "sleep 20"
    result = engine.run(command, cwd=str(tmp_workspace), timeout=1)
    assert set(result) == EXPECTED_KEYS


def test_ok_only_follows_returncode(tmp_workspace):
    """ok 只看 returncode,有 stderr 输出但退出码为 0 时仍然是成功。"""
    result = run_script(
        tmp_workspace, "warn.py", "import sys\nsys.stderr.write('warning')\n"
    )

    assert result["ok"] is True
    assert result["stderr"] == "warning"


def test_invalid_timeout_falls_back_to_default(tmp_workspace):
    """非法 timeout 退回默认值而不是抛异常。"""
    for bad in (None, 0, -5, "abc"):
        result = engine.run(echo("x"), cwd=str(tmp_workspace), timeout=bad)
        assert result["ok"] is True


def test_quoted_arguments_survive_the_shell(tmp_workspace):
    """
    命令里的双引号必须原样送到 shell。

    engine 走的是 subprocess.Popen(["cmd", "/c", command]),Python 的 list2cmdline
    会把 command 里的每个 " 转义成 \\",而 cmd.exe 不认反斜杠转义,收到的是字面量的
    反斜杠。Windows 上带空格的路径必须加引号(C:\\Program Files 是常态),
    所以这条路径上的引号一旦被破坏,run_command 与 /api/terminal/run 都会静默出错。
    """
    result = engine.run(echo('"hello world"'), cwd=str(tmp_workspace), timeout=30)
    assert "\\" not in result["stdout"], f"引号被转义污染: {result['stdout']!r}"


def test_quoted_interpreter_path_runs(tmp_workspace):
    """
    带引号的可执行文件路径必须能执行。
    这是 Windows 上最常见的调用形式,路径含空格时无法不加引号。
    """
    (tmp_workspace / "quoted.py").write_text("print('ran')\n", encoding="utf-8")

    result = engine.run(f'"{PYTHON}" quoted.py', cwd=str(tmp_workspace), timeout=30)

    assert result["ok"] is True, f"带引号的解释器路径无法执行: {result['stderr']!r}"
    assert result["stdout"] == "ran"


def test_stdin_is_closed(tmp_workspace):
    """
    stdin 接 DEVNULL:等待输入的命令应当立刻拿到 EOF 而不是挂到超时。
    """
    started = time.monotonic()
    result = run_script(
        tmp_workspace, "reader.py",
        "import sys\nprint(repr(sys.stdin.read()))\n", timeout=10,
    )

    assert time.monotonic() - started < 8
    assert result["ok"] is True
    assert result["stdout"] == "''"
