"""
执行 shell 命令并返回结果。
- Windows: cmd /c <command>
- POSIX:   /bin/sh -c <command>
"""
import os
import subprocess
import sys

_DEFAULT_TIMEOUT = 30
_MAX_OUTPUT = 64 * 1024  # 单流最大 64KB


def _shell_prefix() -> list[str]:
    if sys.platform.startswith("win"):
        return ["cmd", "/c"]
    return ["/bin/sh", "-c"]


def run(command: str, cwd: str | None = None, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """
    在 cwd(默认用户主目录)下执行 command,捕获 stdout/stderr。
    Returns: {
      "ok":         bool,            # returncode == 0
      "command":    str,
      "cwd":        str,
      "returncode": int,
      "stdout":     str,
      "stderr":     str,
      "truncated":  bool,
      "error":      str | None,      # 仅在超时/启动失败时存在
    }
    """
    if not command or not command.strip():
        return {"ok": False, "error": "空命令"}

    actual_cwd = cwd or os.path.expanduser("~")
    if not os.path.isdir(actual_cwd):
        return {"ok": False, "error": f"cwd 不存在: {actual_cwd}", "command": command}

    try:
        proc = subprocess.run(
            _shell_prefix() + [command],
            cwd=actual_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        truncated = False
        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT] + "\n...(stdout 截断)"
            truncated = True
        if len(err) > _MAX_OUTPUT:
            err = err[:_MAX_OUTPUT] + "\n...(stderr 截断)"
            truncated = True
        return {
            "ok":         proc.returncode == 0,
            "command":    command,
            "cwd":        actual_cwd,
            "returncode": proc.returncode,
            "stdout":     out.rstrip("\n"),
            "stderr":     err.rstrip("\n"),
            "truncated":  truncated,
            "error":      None,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok":         False,
            "error":      f"执行超时 ({timeout}s)",
            "command":    command,
            "cwd":        actual_cwd,
            "returncode": -1,
        }
    except FileNotFoundError as e:
        return {"ok": False, "error": f"shell 不可用: {e}", "command": command, "cwd": actual_cwd}
    except Exception as e:
        return {
            "ok":         False,
            "error":      f"{type(e).__name__}: {e}",
            "command":    command,
            "cwd":        actual_cwd,
            "returncode": -1,
        }
