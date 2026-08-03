"""
全局文件内容搜索:遍历目录树,在文本文件中查找包含 query 的行。
- 自动跳过 .git / node_modules / __pycache__ / .venv / dist / build 等大目录
- 跳过大于 1MB 的文件、按扩展名排除的二进制、以及含 NUL 字节的文件
- 最多收集 MAX_RESULTS 条匹配,避免单次响应过大
- 有墙钟超时和扫描文件数上限,不会在 /sdcard 这类大目录上无限期跑下去
- total 是实际统计到的匹配总数(可能大于返回条数);exact 标记该数字是否已扫完全树
"""
import os
import time

try:
    import sandbox
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import sandbox


MAX_RESULTS   = 200
MAX_FILE_SIZE = 1 * 1024 * 1024
MAX_LINE_LEN  = 400
MAX_FILES     = 20000
TIMEOUT_SEC   = 5.0

_SKIP_DIRS = {
    '.git', '.hg', '.svn',
    'node_modules', '__pycache__', '.venv', 'venv', 'env',
    'dist', 'build', 'out', '.next', '.nuxt',
    '.idea', '.vscode', '.pytest_cache', '.mypy_cache',
    '__pypackages__',
}

# 按扩展名粗略判断是否值得扫;二进制/压缩文件直接跳过
_SKIP_EXTS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.svg',
    '.pdf', '.zip', '.tar', '.gz', '.bz2', '.7z', '.rar',
    '.exe', '.dll', '.so', '.dylib', '.class', '.o', '.obj',
    '.mp3', '.mp4', '.mov', '.avi', '.wav', '.flac',
    '.ttf', '.otf', '.woff', '.woff2',
    '.pyc', '.pyo', '.pyd', '.lock',
}


def _empty(root: str, query: str) -> dict:
    """空查询的统一返回体。"""
    return {
        'root':          root,
        'query':         query,
        'total':         0,
        'returned':      0,
        'truncated':     False,
        'exact':         True,
        'stopped':       'done',
        'scanned_files': 0,
        'elapsed_ms':    0,
        'results':       [],
    }


def _scan_file(full: str, q_lower: str):
    """
    扫单个文件,返回该文件内所有匹配 [(行号, 片段), ...]。
    读不到 / 超大 / 二进制 一律返回空列表,不中断整体搜索。
    """
    try:
        with open(full, 'rb') as f:
            data = f.read(MAX_FILE_SIZE + 1)
    except OSError:
        return []
    if len(data) > MAX_FILE_SIZE or b'\x00' in data:
        return []
    try:
        text = data.decode('utf-8', errors='ignore')
    except UnicodeError:
        return []

    hits = []
    for ln_idx, raw in enumerate(text.splitlines(), 1):
        if q_lower in raw.lower():
            snippet = raw
            if len(snippet) > MAX_LINE_LEN:
                snippet = snippet[:MAX_LINE_LEN] + '…'
            hits.append((ln_idx, snippet))
    return hits


def search(root: str, query: str, max_results: int = MAX_RESULTS, *,
           timeout: float = TIMEOUT_SEC, max_files: int = MAX_FILES,
           should_stop=None) -> dict:
    """
    在 root 下递归搜索包含 query 的行(子串匹配,大小写不敏感)。

    max_results  最多收集多少条匹配片段
    timeout      墙钟超时(秒),超时后停止遍历并把 exact 置为 False
    max_files    最多扫描多少个文件,防止在超大目录树上失控
    should_stop  可选的无参回调,返回 True 时提前中断

    返回 {root, query, total, returned, truncated, exact, stopped,
          scanned_files, elapsed_ms, results: [{path, line, snippet}]}
    total 为实际统计到的匹配总数;exact=False 表示因超时/上限提前停止,
    total 只是下界。
    """
    if not query:
        return _empty(root, query)
    root = sandbox.resolve(root)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"目录不存在: {root}")

    q_lower = query.lower()
    out: list[dict] = []
    total = 0
    scanned = 0
    stopped = 'done'
    started = time.monotonic()
    deadline = started + timeout if timeout and timeout > 0 else None

    for dirpath, dirnames, filenames in os.walk(root):
        # 剪枝:跳过无用目录和隐藏目录(改 in-place,os.walk 会尊重)
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith('.')
        ]

        for name in filenames:
            if deadline is not None and time.monotonic() > deadline:
                stopped = 'timeout'
                break
            if scanned >= max_files:
                stopped = 'max_files'
                break
            if should_stop is not None and should_stop():
                stopped = 'cancelled'
                break

            ext = os.path.splitext(name)[1].lower()
            if ext in _SKIP_EXTS:
                continue
            full = os.path.join(dirpath, name)
            scanned += 1

            hits = _scan_file(full, q_lower)
            if not hits:
                continue
            total += len(hits)
            if len(out) >= max_results:
                continue
            rel = os.path.relpath(full, root).replace('\\', '/')
            for ln_idx, snippet in hits:
                out.append({'path': rel, 'line': ln_idx, 'snippet': snippet})
                if len(out) >= max_results:
                    break

        if stopped != 'done':
            break

    exact = stopped == 'done'
    return {
        'root':          root,
        'query':         query,
        'total':         total,
        'returned':      len(out),
        'truncated':     len(out) < total or not exact,
        'exact':         exact,
        'stopped':       stopped,
        'scanned_files': scanned,
        'elapsed_ms':    int((time.monotonic() - started) * 1000),
        'results':       out,
    }
