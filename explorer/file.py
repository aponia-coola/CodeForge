"""
文件读写与目录列举。

- 所有对外的路径入口都过 sandbox.resolve(),相对路径基于当前工作目录,越界抛 SandboxError
- 写入一律走「临时文件 -> flush -> os.fsync -> os.replace」的原子路径
- 读不加锁,只有写才加排他锁;锁文件放系统临时目录,不污染用户工作区
- 文本读取自动探测编码与主导行尾,写回时保持一致
"""
import contextlib
import hashlib
import locale
import os
import sys
import tempfile
import threading
import time

try:
    import sandbox
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import sandbox


# ════════════════════════════════════════════════════════════
#                        限额与常量
# ════════════════════════════════════════════════════════════
MAX_TEXT_BYTES   = 2 * 1024 * 1024
MAX_LIST_ENTRIES = 5000
DEFAULT_ENCODING = 'utf-8'

_BOM = '\ufeff'
_LOCK_DIR = os.path.join(tempfile.gettempdir(), 'codeforge-locks')


# ════════════════════════════════════════════════════════════
#              跨平台文件锁(FileLock)
# ════════════════════════════════════════════════════════════
# 只用于写入路径,读取不加锁。
# 用 O_EXCL 创建 .lock 文件实现原子加锁,锁文件按目标路径的 hash 命名,
# 统一放在系统临时目录,不在目标文件旁边留旁路文件。
# - Windows:O_EXCL 本身就是排他锁,够用
# - Linux/macOS:O_EXCL 仅防并发创建,还需 fcntl.flock 防并发读写
#
# 进程内可重入:同一线程多次锁同一 path 会计数,不真正竞争文件锁。
# 异常保护:.lock 文件残留超过 30s 视为僵尸,自动清理。
def _lock_path_for(path: str) -> str:
    """把目标路径映射成系统临时目录下的锁文件路径。"""
    key = os.path.normcase(os.path.abspath(path))
    digest = hashlib.sha1(key.encode('utf-8', 'surrogatepass')).hexdigest()
    return os.path.join(_LOCK_DIR, digest + '.lock')


class FileLock:
    _thread_local = threading.local()

    def __init__(self, path: str, timeout: float = 5.0, poll: float = 0.05, stale_sec: float = 30.0):
        self.target = path
        self.lock_path = _lock_path_for(path)
        self.timeout = timeout
        self.poll = poll
        self.stale_sec = stale_sec
        self._fd = None

    def _tls(self):
        if not hasattr(self._thread_local, 'counter'):
            self._thread_local.counter = {}
        return self._thread_local.counter

    def acquire(self):
        tls = self._tls()
        if tls.get(self.lock_path, 0) > 0:
            tls[self.lock_path] += 1
            return
        try:
            os.makedirs(_LOCK_DIR, exist_ok=True)
        except OSError:
            pass
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o644,
                )
                break
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.lock_path)
                except OSError:
                    age = 0
                if age > self.stale_sec:
                    try: os.remove(self.lock_path)
                    except OSError: pass
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'获取文件锁超时: {self.target}')
                time.sleep(self.poll)
        if sys.platform != 'win32':
            import fcntl
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            except OSError:
                os.close(self._fd); self._fd = None
                raise
        tls[self.lock_path] = 1

    def release(self):
        tls = self._tls()
        cnt = tls.get(self.lock_path, 0)
        if cnt > 1:
            tls[self.lock_path] = cnt - 1
            return
        if cnt == 1:
            tls.pop(self.lock_path, None)
        if self._fd is None: return
        try:
            if sys.platform != 'win32':
                import fcntl
                try: fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError: pass
        finally:
            os.close(self._fd)
            self._fd = None
            try: os.remove(self.lock_path)
            except OSError: pass

    def __enter__(self): self.acquire(); return self
    def __exit__(self, *args): self.release()


@contextlib.contextmanager
def locked(path: str, timeout: float = 5.0):
    """对文件加写排他锁的便捷上下文管理器(读路径不需要)。"""
    lock = FileLock(path, timeout=timeout)
    lock.acquire()
    try: yield
    finally: lock.release()


# ════════════════════════════════════════════════════════════
#                     编码与行尾探测
# ════════════════════════════════════════════════════════════
def _locale_encoding() -> str:
    """当前环境的首选编码,取不到时退回 utf-8。"""
    try:
        enc = locale.getpreferredencoding(False)
    except Exception:
        enc = ''
    return (enc or DEFAULT_ENCODING).lower()


def _decode(data: bytes) -> tuple[str, str]:
    """
    按 utf-8 -> utf-8-sig -> locale -> latin-1 依次尝试解码。
    返回 (文本, 实际使用的编码名);latin-1 不会失败,兜底一定有结果。
    """
    order = [DEFAULT_ENCODING, 'utf-8-sig']
    loc = _locale_encoding()
    if loc not in order:
        order.append(loc)
    if 'latin-1' not in order:
        order.append('latin-1')
    for enc in order:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if enc == DEFAULT_ENCODING and text.startswith(_BOM):
            return text[len(_BOM):], 'utf-8-sig'
        return text, enc
    return data.decode('latin-1'), 'latin-1'


def detect_newline(text: str, default: str = '\n') -> str:
    """探测文本中的主导行尾,没有任何换行时返回 default。"""
    crlf = text.count('\r\n')
    cr   = text.count('\r') - crlf
    lf   = text.count('\n') - crlf
    if crlf == 0 and cr == 0 and lf == 0:
        return default
    if crlf >= lf and crlf >= cr:
        return '\r\n'
    if cr > lf:
        return '\r'
    return '\n'


def _to_lf(text: str) -> str:
    """把任意行尾统一成 LF。"""
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _read_bytes(full: str) -> bytes:
    """读原始字节,超出大小上限或含 NUL 字节(二进制)时抛 ValueError。"""
    size = os.path.getsize(full)
    if size > MAX_TEXT_BYTES:
        raise ValueError(
            f"文件过大,超过 {MAX_TEXT_BYTES} 字节上限: {full}(实际 {size} 字节)"
        )
    with open(full, 'rb') as fp:
        data = fp.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"文件过大,超过 {MAX_TEXT_BYTES} 字节上限: {full}")
    if b'\x00' in data:
        raise ValueError(f"二进制文件(含 NUL 字节),拒绝按文本读取: {full}")
    return data


# ════════════════════════════════════════════════════════════
#                        原子写入
# ════════════════════════════════════════════════════════════
def _fsync_dir(dirname: str) -> None:
    """把目录项刷盘,保证 rename 本身持久化;Windows 不支持,静默跳过。"""
    if sys.platform == 'win32':
        return
    try:
        fd = os.open(dirname, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(full: str, text: str, encoding: str, newline: str) -> None:
    """临时文件 -> flush -> os.fsync -> os.replace,替换过程对读者原子可见。"""
    dirname = os.path.dirname(full) or '.'
    try:
        mode = os.stat(full).st_mode
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(prefix='.codeforge-', suffix='.tmp', dir=dirname)
    try:
        with os.fdopen(fd, 'w', encoding=encoding, newline=newline) as fp:
            fp.write(text)
            fp.flush()
            os.fsync(fp.fileno())
        if mode is not None:
            try: os.chmod(tmp, mode)
            except OSError: pass
        os.replace(tmp, full)
        tmp = None
    finally:
        if tmp is not None:
            try: os.remove(tmp)
            except OSError: pass
    _fsync_dir(dirname)


def write_text(path, text, *, encoding=DEFAULT_ENCODING, newline='', agent=False):
    """
    原子写入文本,返回写入的绝对路径。
    newline='' 表示按 text 原样写出,不做任何换行转换。
    """
    full = sandbox.resolve(path, write=True, agent=agent)
    with locked(full):
        _atomic_write(full, text, encoding or DEFAULT_ENCODING, newline)
    return full


# ════════════════════════════════════════════════════════════
#                          读取
# ════════════════════════════════════════════════════════════
def read_text_with_encoding(path) -> tuple[str, str]:
    """
    读取文本文件,返回 (原文, 编码名)。
    原文保留原始行尾(不做通用换行转换),编码按 utf-8 -> utf-8-sig -> locale -> latin-1 探测。
    """
    full = sandbox.resolve(path)
    return _decode(_read_bytes(full))


def read_raw(path, *, keep_newline: bool = False):
    """
    读取文件原文(不含行号),用于 patch 搜索替换的基线。
    keep_newline=True 保留原始行尾;缺省统一成 LF,与旧调用点的写回方式保持一致。
    """
    text, _ = read_text_with_encoding(path)
    return text if keep_newline else _to_lf(text)


def read(path, start_line=None):
    """
    读取文件并逐行加行号返回(给模型/前端展示用)。
    相对路径基于当前工作目录解析,路径越界抛 SandboxError。
    start_line 缺省从第 1 行开始(1-indexed,包含)。
    """
    text, _ = read_text_with_encoding(path)
    lines = _to_lf(text).splitlines(keepends=True)

    s = 0 if start_line is None else max(0, start_line - 1)
    selected = lines[s:]
    total = s + len(selected)
    width = len(str(total)) if total > 0 else 1
    return ''.join(f"{s + i + 1:>{width}} | {line}" for i, line in enumerate(selected))


def listdir_names(path):
    """返回目录下的原始名称列表(os.listdir 的沙箱包装,不带任何元数据)。"""
    return os.listdir(sandbox.resolve(path))


def list_dir(path, show_hidden=True, *, max_entries=MAX_LIST_ENTRIES):
    """
    列出目录下的文件夹与文件。
    用 os.scandir 一次拿到类型与 stat,单个条目出错不中断整体列举。
    条目数超过 max_entries 时截断,并把 truncated 置为 True。
    返回 {path, folders, files, count, limit, truncated}。
    """
    abs_path = sandbox.resolve(path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"路径不存在: {abs_path}")
    if not os.path.isdir(abs_path):
        raise NotADirectoryError(f"不是目录: {abs_path}")

    folders, files = [], []
    count, truncated = 0, False
    it = os.scandir(abs_path)
    try:
        for entry in it:
            name = entry.name
            if not show_hidden and name.startswith('.'):
                continue
            if count >= max_entries:
                truncated = True
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    folders.append({"name": name, "path": entry.path})
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    files.append({
                        "name":  name,
                        "path":  entry.path,
                        "size":  st.st_size,
                        "mtime": st.st_mtime,
                    })
                elif entry.is_symlink():
                    if _symlink_is_dir(entry):
                        folders.append({"name": name, "path": entry.path})
                    else:
                        files.append(_symlink_file_entry(entry))
                else:
                    continue
            except OSError:
                continue
            count += 1
    except OSError:
        pass
    finally:
        it.close()

    folders.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return {
        "path":      abs_path,
        "folders":   folders,
        "files":     files,
        "count":     count,
        "limit":     max_entries,
        "truncated": truncated,
    }


def _symlink_is_dir(entry) -> bool:
    """跟随符号链接判断目标是否为目录,断链按非目录处理。"""
    try:
        return entry.is_dir()
    except OSError:
        return False


def _symlink_file_entry(entry) -> dict:
    """把符号链接整理成文件条目,断链时大小/时间退化为 0。"""
    try:
        st = entry.stat()
    except OSError:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            st = None
    return {
        "name":  entry.name,
        "path":  entry.path,
        "size":  st.st_size if st else 0,
        "mtime": st.st_mtime if st else 0,
    }


# ════════════════════════════════════════════════════════════
#                          写入
# ════════════════════════════════════════════════════════════
def create(file_path, content='', *, encoding=DEFAULT_ENCODING, agent=False):
    """创建文件并写入 content(缺省为空文件),走原子写入路径。"""
    full = sandbox.resolve(file_path, write=True, agent=agent)
    with locked(full):
        _atomic_write(full, content or '', encoding, '')


def change(file_path, content, mode='edit', position=None, end_line=None, chunk_size=4096, *, agent=False):
    """
    修改文件内容。

    mode:
        'edit'   - 按行替换(默认),通过 position/end_line 指定替换范围
        'append' - 追加到文件末尾
    position:
        edit 模式下必填,起始行号(0-indexed,包含)
    end_line:
        结束行号(不包含),默认 position + 1,即只替换 position 这一行
        end_line == position 时为纯插入(不替换任何行)

    读写使用同一编码,并保持文件原有的主导行尾。
    """
    full = sandbox.resolve(file_path, write=True, agent=agent)

    if mode == 'edit':
        if position is None:
            raise ValueError("edit 模式需要指定 position(行号)")
        if end_line is None:
            end_line = position + 1
        with locked(full):
            text, encoding = _decode(_read_bytes(full))
            nl = detect_newline(text)
            lines = _to_lf(text).splitlines(keepends=True)
            segment = _to_lf(content)
            if not segment.endswith('\n'):
                segment += '\n'
            lines[position:end_line] = [segment]
            merged = ''.join(lines)
            if nl != '\n':
                merged = merged.replace('\n', nl)
            _atomic_write(full, merged, encoding, '')
    elif mode == 'append':
        with locked(full):
            _append(full, content, chunk_size)
    else:
        raise ValueError(f"不支持的 mode: {mode},可选 'edit' 或 'append'")


def _append(full: str, content: str, chunk_size: int) -> None:
    """追加写:文件在大小上限内走原子重写,超限则直接追加并 fsync。"""
    try:
        size = os.path.getsize(full)
    except OSError:
        size = 0
    if size <= MAX_TEXT_BYTES:
        try:
            text, encoding = _decode(_read_bytes(full))
        except FileNotFoundError:
            text, encoding = '', DEFAULT_ENCODING
        nl = detect_newline(text)
        tail = _to_lf(content)
        if nl != '\n':
            tail = tail.replace('\n', nl)
        _atomic_write(full, text + tail, encoding, '')
        return
    with open(full, 'a', encoding=DEFAULT_ENCODING, newline='') as fp:
        for i in range(0, len(content), chunk_size):
            fp.write(content[i:i + chunk_size])
        fp.flush()
        os.fsync(fp.fileno())


def remove_file(file_path, *, agent=False):
    """
    删除指定文件(不存在/不是文件会报错)。
    路径先过 sandbox 校验,删除后把父目录刷盘,保证删除本身持久化。
    """
    full = sandbox.resolve(file_path, write=True, agent=agent)
    with locked(full):
        if not os.path.exists(full):
            raise FileNotFoundError(f"文件不存在: {full}")
        if not os.path.isfile(full):
            raise NotADirectoryError(f"不是文件: {full}")
        os.remove(full)
        _fsync_dir(os.path.dirname(full) or '.')


def apply_patches_content(content, patches):
    """
    对原文 content 应用一组搜索替换 patch。
    patches: [{"old": "...", "new": "..."}, ...]
    每个 patch 的 old 必须在当前内容中唯一匹配,否则报错。
    返回应用后的完整内容。
    """
    result = content
    for i, p in enumerate(patches):
        old = p.get("old", "")
        new = p.get("new", "")
        if not old:
            raise ValueError(f"patch #{i+1}: old 为空")
        count = result.count(old)
        if count == 0:
            raise ValueError(f"patch #{i+1}: 未找到匹配的代码块,可能已被其他 patch 修改")
        if count > 1:
            raise ValueError(f"patch #{i+1}: 匹配到 {count} 处,需要更精确的上下文(要求唯一匹配)")
        result = result.replace(old, new, 1)
    return result
