"""
轻量 Git 操作封装:status / diff / stage / unstage / discard / commit / push。
基于 subprocess + git CLI,不引入额外依赖。

包名说明:
本包目录名为 git,会 shadow PyPI 上 GitPython 的导入名(GitPython 装上后也是
`import git`)。本项目全程只用 git CLI,不依赖 GitPython,所以目前无冲突。
将来若真要用 GitPython,必须改本包的目录名,而不是在这里做 import 技巧 ——
改名会波及 main.py、agent 等全部调用点,属于跨文件改动。

统一返回契约:
除 is_repo() 外,所有对外函数一律返回 GitResult(dataclass),不再有
(bool, str) / (bool, str, str) / 直接抛 RuntimeError 三种写法混用的情况。
    ok:     bool   命令是否成功
    output: str    正常输出(diff 原文 / git 的 stdout)
    error:  str    失败原因,ok=True 时为空串
    data:   dict   结构化附加信息(status 的分支与改动列表、commit 的 hash)
GitResult.as_dict() 把 data 摊平到顶层,可直接交给 flask.jsonify。
所有对外函数都不抛异常:路径越界、目录不存在、git 缺失、超时都收敛成
ok=False 的 GitResult。

其余约定:
- cwd 一律先过 sandbox.resolve(),越界直接拒绝执行。
- 所有 subprocess 调用都带 encoding='utf-8'、errors='replace' 和 timeout。
- stdin 接 DEVNULL 且 GIT_TERMINAL_PROMPT=0:push 遇到要凭据的远端会立刻失败,
  而不是挂在提示上等到超时。
"""
import os
import re
import subprocess
from dataclasses import dataclass, field

import sandbox

_DEFAULT_TIMEOUT = 15
_PUSH_TIMEOUT = 60


# ════════════════════════════════════════════════════════════
#                        统一返回值
# ════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class GitResult:
    """git 操作的统一返回值,详见模块 docstring 的返回契约。"""
    ok: bool
    output: str = ''
    error: str = ''
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """摊平成 JSON 友好的 dict:{ok, output, error, **data}。"""
        return {'ok': self.ok, 'output': self.output, 'error': self.error, **self.data}


def _ok(output: str = '', **data) -> GitResult:
    """构造成功结果。"""
    return GitResult(True, output, '', dict(data))


def _fail(error: str, output: str = '', **data) -> GitResult:
    """构造失败结果。"""
    return GitResult(False, output, error, dict(data))


# ════════════════════════════════════════════════════════════
#                      子进程与路径校验
# ════════════════════════════════════════════════════════════

def _env() -> dict:
    """
    关掉交互式凭据提示和分页器。
    否则 push 到需要账号密码的远端时,git 会一直等输入直到 timeout 才被打断。
    """
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_PAGER'] = 'cat'
    return env


def _run(cwd: str, *args: str, timeout: int = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """执行一条 git 命令。encoding/errors/timeout 三件套齐全,解码失败不抛异常。"""
    return subprocess.run(
        ['git', *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout,
        env=_env(),
    )


def _capture(cwd: str, *args: str, timeout: int = _DEFAULT_TIMEOUT):
    """
    执行 git 并把异常收敛掉。
    返回 (CompletedProcess, None) 或 (None, 失败的 GitResult)。
    """
    try:
        return _run(cwd, *args, timeout=timeout), None
    except FileNotFoundError:
        return None, _fail('找不到 git 可执行文件,请确认已安装并在 PATH 中')
    except subprocess.TimeoutExpired:
        name = args[0] if args else ''
        return None, _fail(f'git {name} 超时 ({timeout}s)')
    except OSError as e:
        return None, _fail(f'{type(e).__name__}: {e}')


def _prepare(cwd: str):
    """
    校验工作目录:先过 sandbox,再确认是存在的目录。
    返回 (绝对路径, None) 或 ('', 失败的 GitResult)。
    """
    try:
        real = sandbox.resolve(cwd)
    except sandbox.SandboxError as e:
        return '', _fail(f'工作目录不允许: {e}')
    except (OSError, ValueError) as e:
        return '', _fail(f'工作目录无法解析: {e}')
    if not os.path.isdir(real):
        return '', _fail(f'工作目录不存在: {real}')
    return real, None


def _stderr_of(p: subprocess.CompletedProcess, fallback: str) -> str:
    """从 CompletedProcess 里取一条可读的失败原因。"""
    return ((p.stderr or '') or (p.stdout or '') or fallback).strip()


# ════════════════════════════════════════════════════════════
#                        状态与差异
# ════════════════════════════════════════════════════════════

_STAGE_MAP = {
    'M': 'modified', 'A': 'added', 'D': 'deleted',
    'R': 'renamed', 'C': 'copied', 'U': 'unmerged',
}
_WT_MAP = {
    'M': 'modified', 'D': 'deleted', 'A': 'added', 'U': 'unmerged',
}


def _classify(x: str, y: str) -> tuple[str, bool]:
    """根据 XY 两字符返回 (status 字符串, staged 标志)。"""
    staged_part = _STAGE_MAP.get(x, '') if x != ' ' else ''
    wt_part     = _WT_MAP.get(y, '')     if y != ' ' else ''
    if staged_part and wt_part:
        return f"{staged_part}+{wt_part}", True
    if staged_part:
        return staged_part, True
    if wt_part:
        return wt_part, False
    return 'unknown', False


def _parse_status(text: str) -> dict:
    """把 `git status --porcelain=v1 -b` 的输出解析成 {branch, ahead, behind, changes}。"""
    branch, ahead, behind = '', 0, 0
    changes: list[dict] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith('## '):
            m = re.match(r'([^\s.]+?)(?:\.\.\.([^\s]+))?(?: \[(.+)\])?$', line[3:])
            if m:
                branch = m.group(1)
                ab = m.group(3) or ''
                am = re.search(r'ahead (\d+)', ab)
                bm = re.search(r'behind (\d+)', ab)
                ahead  = int(am.group(1)) if am else 0
                behind = int(bm.group(1)) if bm else 0
        elif line.startswith('?? '):
            changes.append({'path': line[3:], 'status': 'untracked', 'staged': False, 'x': '?', 'y': '?'})
        elif len(line) >= 3 and line[2] == ' ':
            x, y, path = line[0], line[1], line[3:]
            status_str, staged = _classify(x, y)
            changes.append({'path': path, 'status': status_str, 'staged': staged, 'x': x, 'y': y})
    return {'branch': branch, 'ahead': ahead, 'behind': behind, 'changes': changes}


def is_repo(cwd: str) -> bool:
    """
    cwd 是否位于一个 git 工作树内。
    这是唯一不返回 GitResult 的对外函数:它是个谓词,任何失败(路径越界、目录不存在、
    git 缺失、超时)都归为 False,调用方按「不是仓库」处理即可,不需要 try。
    """
    real, bad = _prepare(cwd)
    if bad is not None:
        return False
    p, bad = _capture(real, 'rev-parse', '--is-inside-work-tree')
    if bad is not None:
        return False
    return p.returncode == 0 and (p.stdout or '').strip() == 'true'


def status(cwd: str) -> GitResult:
    """
    `git status --porcelain=v1 -b`。
    成功:ok=True,data={'branch': str, 'ahead': int, 'behind': int, 'changes': list[dict]}
    失败:ok=False,error 为 git 的报错(不是仓库也走这里)
    """
    real, bad = _prepare(cwd)
    if bad is not None:
        return bad
    p, bad = _capture(real, 'status', '--porcelain=v1', '-b')
    if bad is not None:
        return bad
    if p.returncode != 0:
        return _fail(_stderr_of(p, 'git status 失败'))
    return _ok('', **_parse_status(p.stdout or ''))


def diff(cwd: str, path: str, staged: bool = False) -> GitResult:
    """
    `git diff [--staged] -- <path>`。
    成功:ok=True,output 为 unified diff 原文(保留首尾空白,不做 strip)。
    没有差异时 output 为空串,ok 仍是 True。
    """
    real, bad = _prepare(cwd)
    if bad is not None:
        return bad
    if not path:
        return _fail('缺少 path')
    args = ['diff']
    if staged:
        args.append('--staged')
    args += ['--', path]
    p, bad = _capture(real, *args)
    if bad is not None:
        return bad
    if p.returncode != 0:
        return _fail(_stderr_of(p, 'git diff 失败'))
    return _ok(p.stdout or '')


# ════════════════════════════════════════════════════════════
#                     暂存 / 撤销 / 提交
# ════════════════════════════════════════════════════════════

def stage(cwd: str, path: str) -> GitResult:
    """`git add -- <path>`。整仓暂存请用 stage_all(),不要传 '-A'。"""
    real, bad = _prepare(cwd)
    if bad is not None:
        return bad
    if not path:
        return _fail('缺少 path')
    p, bad = _capture(real, 'add', '--', path)
    if bad is not None:
        return bad
    if p.returncode != 0:
        return _fail(_stderr_of(p, 'git add 失败'))
    return _ok((p.stdout or '').strip())


def stage_all(cwd: str) -> GitResult:
    """`git add -A`,暂存全部改动。"""
    real, bad = _prepare(cwd)
    if bad is not None:
        return bad
    p, bad = _capture(real, 'add', '-A')
    if bad is not None:
        return bad
    if p.returncode != 0:
        return _fail(_stderr_of(p, 'git add -A 失败'))
    return _ok((p.stdout or '').strip())


def unstage(cwd: str, path: str) -> GitResult:
    """`git reset HEAD -- <path>`。整仓取消暂存请用 unstage_all()。"""
    real, bad = _prepare(cwd)
    if bad is not None:
        return bad
    if not path:
        return _fail('缺少 path')
    p, bad = _capture(real, 'reset', 'HEAD', '--', path)
    if bad is not None:
        return bad
    if p.returncode != 0:
        return _fail(_stderr_of(p, 'git reset 失败'))
    return _ok((p.stdout or '').strip())


def unstage_all(cwd: str) -> GitResult:
    """`git reset`,取消暂存全部改动。"""
    real, bad = _prepare(cwd)
    if bad is not None:
        return bad
    p, bad = _capture(real, 'reset')
    if bad is not None:
        return bad
    if p.returncode != 0:
        return _fail(_stderr_of(p, 'git reset 失败'))
    return _ok((p.stdout or '').strip())


def discard(cwd: str, path: str) -> GitResult:
    """
    放弃工作区修改。
    已跟踪文件走 `git checkout -- <path>`,未跟踪文件走 `git clean -f -- <path>`。
    """
    real, bad = _prepare(cwd)
    if bad is not None:
        return bad
    if not path:
        return _fail('缺少 path')
    st = status(real)
    if not st.ok:
        return st
    target = next((c for c in st.data.get('changes', []) if c['path'] == path), None)
    if target and target['x'] == '?':
        p, bad = _capture(real, 'clean', '-f', '--', path)
    else:
        p, bad = _capture(real, 'checkout', '--', path)
    if bad is not None:
        return bad
    if p.returncode != 0:
        return _fail(_stderr_of(p, 'git discard 失败'))
    return _ok((p.stdout or '').strip())


def commit(cwd: str, message: str) -> GitResult:
    """
    `git commit -m <message>`。
    成功:ok=True,output 为 git 的 stdout,data={'hash': 短 hash}。
    取不到 hash 时 data['hash'] 为空串;失败时同样带 data={'hash': ''}。
    """
    real, bad = _prepare(cwd)
    if bad is not None:
        return bad
    if not message or not message.strip():
        return _fail('提交信息为空', hash='')
    p, bad = _capture(real, 'commit', '-m', message)
    if bad is not None:
        return _fail(bad.error, hash='')
    if p.returncode != 0:
        return _fail(_stderr_of(p, 'git commit 失败'), hash='')
    h, hbad = _capture(real, 'rev-parse', '--short', 'HEAD')
    short = '' if hbad is not None or h.returncode != 0 else (h.stdout or '').strip()
    return _ok((p.stdout or '').strip(), hash=short)


def push(cwd: str, remote: str = '', branch: str = '') -> GitResult:
    """
    `git push [<remote> [<branch>]]`,远程/分支留空则用当前 upstream。超时 60s。
    git 把进度写在 stderr,所以成功时 output 是 stdout 与 stderr 的合并文本。
    """
    real, bad = _prepare(cwd)
    if bad is not None:
        return bad
    args = ['push']
    if remote:
        args.append(remote)
    if branch:
        args.append(branch)
    p, bad = _capture(real, *args, timeout=_PUSH_TIMEOUT)
    if bad is not None:
        return bad
    text = '\n'.join(s for s in ((p.stdout or '').strip(), (p.stderr or '').strip()) if s)
    if p.returncode != 0:
        return _fail(text or 'git push 失败')
    return _ok(text)
