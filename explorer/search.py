"""
全局文件内容搜索:遍历目录树,在文本文件中查找包含 query 的行。
- 自动跳过 .git / node_modules / __pycache__ / .venv / dist / build 等大目录
- 跳过大于 1MB 的二进制/巨型文件
- 最多返回 MAX_RESULTS 条匹配,避免单次响应过大
"""
import os


MAX_RESULTS   = 200
MAX_FILE_SIZE = 1 * 1024 * 1024   # 1 MB
MAX_LINE_LEN  = 400

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


def search(root: str, query: str, max_results: int = MAX_RESULTS) -> dict:
    """
    返回 {root, query, total, truncated, results: [{path, line, snippet}]}
    """
    if not query:
        return {'root': root, 'query': '', 'total': 0, 'truncated': False, 'results': []}
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"目录不存在: {root}")

    q_lower = query.lower()
    out: list[dict] = []
    total = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        # 剪枝:跳过无用目录(改 in-place,os.walk 会尊重)
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith('.') or d == '.']

        for name in filenames:
            if len(out) >= max_results:
                truncated = True
                break
            ext = os.path.splitext(name)[1].lower()
            if ext in _SKIP_EXTS:
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > MAX_FILE_SIZE:
                    continue
                with open(full, 'r', encoding='utf-8', errors='ignore') as f:
                    for ln_idx, raw in enumerate(f, 1):
                        if q_lower in raw.lower():
                            snippet = raw.rstrip('\n')
                            if len(snippet) > MAX_LINE_LEN:
                                snippet = snippet[:MAX_LINE_LEN] + '…'
                            out.append({
                                'path':    os.path.relpath(full, root).replace('\\', '/'),
                                'line':    ln_idx,
                                'snippet': snippet,
                            })
                            total += 1
                            if len(out) >= max_results:
                                break
            except (OSError, UnicodeDecodeError):
                continue

        if len(out) >= max_results:
            truncated = True
            break

    return {
        'root':      root,
        'query':     query,
        'total':     total,
        'truncated': truncated,
        'results':   out,
    }
