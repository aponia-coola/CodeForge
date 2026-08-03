"""
执行 shell 命令并返回结果。
- Windows: cmd /c <command>
- POSIX:   /bin/sh -c <command>

三个历史坑与当前的处理方式:
1. 解码。subprocess 的 text=True 在 subprocess 自己的读取线程里解码,子进程输出
   UTF-8 而系统 locale 是 cp936 时,UnicodeDecodeError 抛在那个守护线程里,run()
   的 except 根本捕获不到,最终返回 ok=True / returncode=0 / stdout='',调用方会
   误判「命令没有输出」。现在一律 text=False 拿 bytes,自己按
   utf-8 → locale → errors='replace' 的顺序解码,降级情况写进返回值的 error 字段。
2. 超时。subprocess.run(timeout=) 只终止直接子进程,shell 派生的孙进程继续持有
   管道,communicate 要等到孙进程退出才返回(实测 timeout=2 的命令 25 秒才回来)。
   现在用 Popen 把子进程拉进独立进程组(POSIX: start_new_session,
   Windows: CREATE_NEW_PROCESS_GROUP),超时后按组终止(os.killpg / taskkill /T /F),
   收尾的 communicate 也带超时,防止管道卡死。
3. 工作目录。cwd 一律先过 sandbox.resolve(),越界返回 ok=False 而不是抛异常。

另外 stdin 接 DEVNULL:等待输入的交互式命令会立刻拿到 EOF,而不是挂到超时。

返回值的键固定为 ok / command / cwd / returncode / stdout / stderr / truncated / error,
main.py 与 agent/tool.py 都依赖它们,所有分支(含各种错误分支)都返回完整的这 8 个键。
"""
import locale
import os
import signal
import subprocess
import sys

import sandbox

_DEFAULT_TIMEOUT = 30
# 单流最大 64KB
_MAX_OUTPUT = 64 * 1024
# 超时终止后,给收尾的 communicate / taskkill 的宽限秒数
_KILL_GRACE = 2

_IS_WINDOWS = sys.platform.startswith("win")


# ════════════════════════════════════════════════════════════
#                        输出解码
# ════════════════════════════════════════════════════════════

def _candidate_encodings() -> list[str]:
    """解码候选编码表:utf-8 优先,其次系统 locale 编码。"""
    out = ["utf-8"]
    for enc in (locale.getpreferredencoding(False), sys.getdefaultencoding()):
        if enc and enc.lower() not in [e.lower() for e in out]:
            out.append(enc)
    return out


def _normalize_newlines(text: str) -> str:
    """把 \\r\\n 和单独的 \\r 统一成 \\n,保持和原来 text=True 的换行转换一致。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode(raw: bytes) -> tuple[str, bool]:
    """
    把子进程输出的 bytes 解码成文本,返回 (文本, 是否降级)。
    降级 = 所有候选编码都失败,最终用 errors='replace' 兜底,文本里会出现替换字符。
    """
    if not raw:
        return "", False
    for enc in _candidate_encodings():
        try:
            return _normalize_newlines(raw.decode(enc)), False
        except (UnicodeDecodeError, LookupError):
            continue
    return _normalize_newlines(raw.decode("utf-8", errors="replace")), True


# ════════════════════════════════════════════════════════════
#                      进程组与超时终止
# ════════════════════════════════════════════════════════════

# 必须用 shell=True 而不是 ["cmd", "/c", command] 这种列表形式。
# Windows 上 subprocess 的 list2cmdline 会把命令里的 " 转义成 \",而 cmd.exe 不认
# 反斜杠转义,收到的是字面量反斜杠:echo "hello world" 会输出 \"hello world\",
# 带空格的解释器路径("C:\Program Files\...\python.exe" x.py)则直接无法执行。
# 实测三种方式,只有 shell=True 两种场景都正确:Python 会拼成 cmd.exe /c "<命令>",
# 多出的外层引号正好触发 cmd 正确的引号剥离规则。
# POSIX 下 shell=True 等价于 ["/bin/sh", "-c", command],与原行为一致。


def _popen_kwargs() -> dict:
    """让子进程独立成组/成会话,超时后才有办法连孙进程一起终止。"""
    if _IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _win_create_job():
    """
    创建一个 Job Object 用于超时清理。
    taskkill /T 只能顺父子链找进程,而 `start /b` 之类会主动脱离父进程,
    杀了 cmd.exe 孙进程照样活着继续占管道和端口。Job 按作业归属终止,不受此限。
    不设 KILL_ON_JOB_CLOSE:正常结束时关句柄不应牵连用户有意后台化的进程,
    只有超时分支才显式 TerminateJobObject。
    """
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        job = k32.CreateJobObjectW(None, None)
        return job or None
    except Exception:
        return None


def _win_assign_job(job, proc: subprocess.Popen) -> bool:
    if not job:
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))
    except Exception:
        return False


def _win_close_job(job) -> None:
    if not job:
        return
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle(job)
    except Exception:
        pass


def _kill_tree(proc: subprocess.Popen, job=None) -> None:
    """
    终止整条进程树。
    只 kill 直接子进程是不够的:shell 派生的孙进程会活下来并继续占着管道。
    """
    # Job 必须无条件终止:`start /b` 会让 cmd.exe 立刻退出,而脱离出去的孙进程
    # 还活着占着管道。此时 proc.poll() 已非 None,若在这里早退,孙进程就漏掉了。
    killed = False
    if _IS_WINDOWS and job:
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            killed = bool(k32.TerminateJobObject(job, 1))
        except Exception:
            killed = False
    if proc.poll() is not None:
        return
    if _IS_WINDOWS:
        if not killed:
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=_KILL_GRACE,
                )
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


# ════════════════════════════════════════════════════════════
#                          对外接口
# ════════════════════════════════════════════════════════════

def _result(command: str, cwd: str, returncode: int, stdout: str = "", stderr: str = "",
            truncated: bool = False, error: str | None = None) -> dict:
    """构造统一返回值,保证任何分支都带齐全部 8 个键。ok 只看 returncode。"""
    return {
        "ok":         returncode == 0,
        "command":    command,
        "cwd":        cwd,
        "returncode": returncode,
        "stdout":     stdout,
        "stderr":     stderr,
        "truncated":  truncated,
        "error":      error,
    }


def run(command: str, cwd: str | None = None, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """
    在 cwd(默认用户主目录)下执行 command,捕获 stdout/stderr。
    cwd 先过 sandbox.resolve(),越界或不存在都返回 ok=False,不抛异常。
    Returns: {
      "ok":         bool,            returncode == 0
      "command":    str,
      "cwd":        str,
      "returncode": int,             超时或启动失败为 -1
      "stdout":     str,             超时也会带上已经收到的部分输出
      "stderr":     str,
      "truncated":  bool,
      "error":      str | None,      超时 / 启动失败 / 解码降级时的说明
    }
    """
    if not command or not command.strip():
        return _result(command or "", "", -1, error="空命令")

    raw_cwd = cwd or os.path.expanduser("~")
    try:
        actual_cwd = sandbox.resolve(raw_cwd)
    except sandbox.SandboxError as e:
        return _result(command, str(raw_cwd), -1, error=f"工作目录不允许: {e}")
    if not os.path.isdir(actual_cwd):
        return _result(command, actual_cwd, -1, error=f"cwd 不存在: {actual_cwd}")

    try:
        limit = int(timeout)
    except (TypeError, ValueError):
        limit = _DEFAULT_TIMEOUT
    if limit <= 0:
        limit = _DEFAULT_TIMEOUT

    job = _win_create_job()
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=actual_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_popen_kwargs(),
        )
    except FileNotFoundError as e:
        _win_close_job(job)
        return _result(command, actual_cwd, -1, error=f"shell 不可用: {e}")
    except (OSError, ValueError) as e:
        _win_close_job(job)
        return _result(command, actual_cwd, -1, error=f"{type(e).__name__}: {e}")

    _win_assign_job(job, proc)

    timed_out = False
    try:
        out_raw, err_raw = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc, job)
        try:
            out_raw, err_raw = proc.communicate(timeout=_KILL_GRACE)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out_raw, err_raw = b"", b""
    except Exception as e:
        _kill_tree(proc, job)
        _win_close_job(job)
        return _result(command, actual_cwd, -1, error=f"{type(e).__name__}: {e}")
    finally:
        _win_close_job(job)

    out, out_lossy = _decode(out_raw or b"")
    err, err_lossy = _decode(err_raw or b"")

    truncated = False
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + "\n...(stdout 截断)"
        truncated = True
    if len(err) > _MAX_OUTPUT:
        err = err[:_MAX_OUTPUT] + "\n...(stderr 截断)"
        truncated = True

    notes = []
    if timed_out:
        notes.append(f"执行超时 ({limit}s),已终止整个进程组")
    if out_lossy or err_lossy:
        notes.append("输出既不是合法 UTF-8 也不符合当前 locale 编码,已按 errors='replace' 解码")

    if timed_out:
        returncode = -1
    else:
        returncode = proc.returncode if proc.returncode is not None else -1

    return _result(
        command,
        actual_cwd,
        returncode,
        stdout=out.rstrip("\n"),
        stderr=err.rstrip("\n"),
        truncated=truncated,
        error="; ".join(notes) or None,
    )
