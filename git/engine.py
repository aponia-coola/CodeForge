"""
轻量 Git 操作封装:status / diff / stage / unstage / commit / discard。
基于 subprocess.run + git CLI,不引入额外依赖。
"""
import re
import subprocess


def _run(cwd: str, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git', *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout,
    )


def is_repo(cwd: str) -> bool:
    p = _run(cwd, 'rev-parse', '--is-inside-work-tree')
    return p.returncode == 0 and (p.stdout or '').strip() == 'true'


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


def status(cwd: str) -> dict:
    """
    `git status --porcelain=v1 -b` → {branch, ahead, behind, changes: [...]}
    不是 git 仓库 → raise RuntimeError (前端走 "不是仓库" UI)。
    """
    p = _run(cwd, 'status', '--porcelain=v1', '-b')
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or 'git status 失败').strip())

    branch, ahead, behind = '', 0, 0
    changes: list[dict] = []
    for raw in (p.stdout or '').splitlines():
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


def diff(cwd: str, path: str, staged: bool = False) -> str:
    """返回 unified diff 文本(供前端 diff viewer 使用)。"""
    args = ['diff']
    if staged:
        args.append('--staged')
    args += ['--', path]
    p = _run(cwd, *args)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or 'git diff 失败').strip())
    return p.stdout or ''


def stage(cwd: str, path: str) -> tuple[bool, str]:
    p = _run(cwd, 'add', '--', path)
    return p.returncode == 0, (p.stderr or '').strip()


def unstage(cwd: str, path: str) -> tuple[bool, str]:
    p = _run(cwd, 'reset', 'HEAD', '--', path)
    return p.returncode == 0, (p.stderr or '').strip()


def discard(cwd: str, path: str) -> tuple[bool, str]:
    """放弃工作区修改(对未跟踪的文件用 git clean -f)。"""
    s = status(cwd)
    target = next((c for c in s['changes'] if c['path'] == path), None)
    if target and target['x'] == '?':
        p = _run(cwd, 'clean', '-f', '--', path)
    else:
        p = _run(cwd, 'checkout', '--', path)
    return p.returncode == 0, (p.stderr or '').strip()


def commit(cwd: str, message: str) -> tuple[bool, str, str]:
    """返回 (ok, stdout/stderr, 提交后 hash 头 7 位)。"""
    p = _run(cwd, 'commit', '-m', message)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or '').strip(), ''
    h = _run(cwd, 'rev-parse', '--short', 'HEAD')
    return True, (p.stdout or '').strip(), (h.stdout or '').strip()
