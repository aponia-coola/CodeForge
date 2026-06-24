"""
SSH 远程会话管理 — 基于 asyncssh。

每个连接 = 一个 SshSession,内部跑一个事件循环线程(因为 asyncssh 是 async,但
Flask 是 sync)。一个 manager 全局管理多 session。

公开 API:
    SshSession   — 单个连接(connect / exec / sftp_ls / sftp_read / sftp_write / close)
    SshManager   — 多 session 管理
    get_manager  — 取全局单例
"""
from __future__ import annotations

import asyncio
import base64
import os
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Any, Optional

import asyncssh


_DEFAULT_TIMEOUT = 20
_CONNECT_TIMEOUT = 15


# ════════════════════════════════════════════════════════════
#                    后台事件循环线程
# ════════════════════════════════════════════════════════════

class _LoopThread:
    """一个独立线程,里面跑 asyncio 事件循环,给 asyncssh 用。

    跨线程提交任务用 submit(coro, timeout) -> Future。
    """
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = False
        self.start()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stopped = False
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name='ssh-loop', daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError('ssh loop thread failed to start')

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    def submit(self, coro, timeout: float = _DEFAULT_TIMEOUT) -> Any:
        """跨线程跑一个协程,返回结果或抛异常。同步等待。"""
        if self._stopped or self.loop is None or not self.loop.is_running():
            raise RuntimeError('ssh loop not running')
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def close(self):
        if self._stopped or self.loop is None:
            return
        self._stopped = True
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass
        # 不 join,daemon thread 退出时自动结束


# ════════════════════════════════════════════════════════════
#                          SshSession
# ════════════════════════════════════════════════════════════

class SshSession:
    """单个 SSH 连接。线程安全,所有操作都是同步阻塞的。"""
    def __init__(self, host: str, port: int, user: str,
                 password: str = '', key_path: str = '',
                 loop_thread: _LoopThread | None = None):
        self.id          = 'ssh_' + uuid.uuid4().hex[:10]
        self.host        = host
        self.port        = int(port or 22)
        self.user        = user or ''
        self._password   = password or ''
        self._key_path   = key_path or ''
        self._loop       = loop_thread or _LoopThread()
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._sftp: Optional[asyncssh.SFTPClient] = None
        self._sftp_lock  = threading.Lock()
        self._cmd_lock   = threading.Lock()
        self._closed     = False
        self.created_at  = time.time()
        self._connect_error: str | None = None

    # ---------- 连接 ----------
    def connect(self, timeout: float = _CONNECT_TIMEOUT):
        """同步打开 SSH 连接,失败抛 RuntimeError。"""
        if self._closed:
            raise RuntimeError('session closed')
        if self._conn and not self._conn.is_closed():
            return

        async def _do_connect():
            client_keys = None
            if self._key_path and os.path.isfile(self._key_path):
                try:
                    client_keys = [asyncssh.read_private_key(self._key_path)]
                except Exception:
                    client_keys = None
            try:
                self._conn = await asyncio.wait_for(
                    asyncssh.connect(
                        self.host,
                        port=self.port,
                        username=self.user,
                        password=self._password or None,
                        client_keys=client_keys,
                        known_hosts=None,  # 简化:不校验指纹(生产应当校验)
                    ),
                    timeout=timeout,
                )
            except Exception as e:
                self._connect_error = f'{type(e).__name__}: {e}'
                raise

        try:
            self._loop.submit(_do_connect(), timeout=timeout + 2)
        except Exception as e:
            msg = self._connect_error or f'{type(e).__name__}: {e}'
            raise RuntimeError(f'SSH 连接失败: {msg}') from e

    def _ensure_sftp(self):
        if self._sftp is not None:
            return self._sftp
        with self._sftp_lock:
            if self._sftp is not None:
                return self._sftp
            if not self._conn:
                raise RuntimeError('not connected')

            async def _open():
                return await self._conn.start_sftp_client()
            self._sftp = self._loop.submit(_open(), timeout=_DEFAULT_TIMEOUT)
            return self._sftp

    # ---------- 信息 ----------
    def info(self) -> dict:
        return {
            'id':         self.id,
            'host':       self.host,
            'port':       self.port,
            'user':       self.user,
            'connected':  bool(self._conn) and not self._conn.is_closed(),
            'closed':     self._closed,
            'created_at': self.created_at,
        }

    # ---------- 跑命令 ----------
    def exec(self, command: str, cwd: str = '', timeout: int = _DEFAULT_TIMEOUT) -> dict:
        """在远端跑一条命令,捕获 stdout/stderr/exit_code。"""
        if not self._conn or self._conn.is_closed():
            return {'ok': False, 'error': 'not connected', 'command': command}
        with self._cmd_lock:
            async def _do_exec():
                # 远端 cd + 命令(用 ; 连起来,避免单独 shell 维持状态)
                full = command if not cwd else f'cd {cwd!r} 2>/dev/null && {command}'
                proc = await self._conn.create_process(full)
                try:
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.terminate()
                    return {'ok': False, 'error': f'timeout ({timeout}s)',
                            'stdout': '', 'stderr': '', 'returncode': -1}
                return {
                    'stdout':     out.decode('utf-8', 'replace') if isinstance(out, (bytes, bytearray)) else (out or ''),
                    'stderr':     err.decode('utf-8', 'replace') if isinstance(err, (bytes, bytearray)) else (err or ''),
                    'returncode': proc.exit_status if proc.exit_status is not None else 0,
                }
            try:
                res = self._loop.submit(_do_exec(), timeout=timeout + 2)
            except Exception as e:
                return {'ok': False, 'error': f'{type(e).__name__}: {e}', 'command': command}
        res['ok']      = res.get('returncode', 0) == 0
        res['command'] = command
        res['stdout']  = (res.get('stdout') or '').rstrip('\n')
        res['stderr']  = (res.get('stderr') or '').rstrip('\n')
        return res

    # ---------- SFTP 文件操作 ----------
    def list_dir(self, path: str) -> dict:
        """列目录。返回 [{name, type, size, mtime, path}]"""
        try:
            sftp = self._ensure_sftp()
            async def _resolve_and_list():
                # asyncssh 2.x: realpath 是协程,要 await
                rp = await sftp.realpath(path) if path else (await sftp.getcwd() or '.')
                return await _async_list(sftp, rp)
            real_items = self._loop.submit(_resolve_and_list(), timeout=_DEFAULT_TIMEOUT)
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}', 'path': path}
        return {
            'ok':    True,
            'path':  path,                # 用户传的原路径(已 resolve)
            'items': real_items,
        }

    def read_file(self, path: str, max_bytes: int = 2 * 1024 * 1024) -> dict:
        try:
            sftp = self._ensure_sftp()
            data = self._loop.submit(_async_read(sftp, path, max_bytes), timeout=_DEFAULT_TIMEOUT)
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}', 'path': path}
        return {
            'ok':     True,
            'path':   path,
            'size':   len(data),
            'binary': False,
            'text':   data.decode('utf-8', 'replace'),
            'b64':    None,
        }

    def write_file(self, path: str, content: str) -> dict:
        try:
            sftp = self._ensure_sftp()
            data = content.encode('utf-8', 'replace')
            self._loop.submit(_async_write(sftp, path, data), timeout=_DEFAULT_TIMEOUT)
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}', 'path': path}
        return {'ok': True, 'path': path, 'size': len(data)}

    # ---------- 关闭 ----------
    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._conn and not self._conn.is_closed():
            try:
                self._loop.submit(self._conn.close(), timeout=3)
            except Exception:
                pass
        self._conn = None
        self._sftp = None


async def _async_list(sftp, path):
    items = []
    # asyncssh: readdir 返回 SFTPName 列表,每个 SFTPName.filename 是名字,.attrs 是 SFTPAttrs
    for entry in await sftp.readdir(path):
        name = entry.filename
        if name in ('.', '..'):
            continue
        full = (path.rstrip('/') + '/' + name) if path != '/' else '/' + name
        a = entry.attrs
        # 优先用 SFTPAttrs.type(SFTPv4+),否则 fallback permissions
        is_dir = False
        if a is not None:
            if getattr(a, 'type', None) is not None:
                # SFTP 协议规定 0o4xxxx = 目录(参照 SFTP 规范的 type 字段)
                is_dir = (a.type & 0o170000) == 0o040000
            else:
                perms = a.permissions or 0
                is_dir = (perms & 0o40000) != 0
        items.append({
            'name':  name,
            'path':  full,
            'type':  'folder' if is_dir else 'file',
            'size':  (a.size if a and a.size else 0) or 0,
            'mtime': (a.mtime if a and a.mtime else 0) or 0,
        })
    items.sort(key=lambda x: (x['type'] != 'folder', x['name'].lower()))
    return items


async def _async_read(sftp, path, max_bytes):
    async with sftp.open(path, 'rb') as f:
        data = await f.read(max_bytes + 1)
    if len(data) > max_bytes:
        data = data[:max_bytes]
    return data


async def _async_write(sftp, path, data):
    import os as _os
    _os; # noop
    async with sftp.open(path, 'wb') as f:
        await f.write(data)


# ════════════════════════════════════════════════════════════
#                          SshManager
# ════════════════════════════════════════════════════════════

class SshManager:
    def __init__(self):
        self._sessions: dict[str, SshSession] = {}
        self._lock      = threading.Lock()
        self._loop      = _LoopThread()  # 共享一个事件循环

    def create(self, host: str, port: int, user: str,
               password: str = '', key_path: str = '') -> SshSession:
        s = SshSession(host, port, user, password=password, key_path=key_path,
                       loop_thread=self._loop)
        s.connect()  # 同步连,失败抛 RuntimeError
        with self._lock:
            self._sessions[s.id] = s
        return s

    def get(self, sid: str) -> Optional[SshSession]:
        with self._lock:
            return self._sessions.get(sid)

    def list(self) -> list[dict]:
        with self._lock:
            return [s.info() for s in self._sessions.values()]

    def destroy(self, sid: str) -> bool:
        with self._lock:
            s = self._sessions.pop(sid, None)
        if not s:
            return False
        s.close()
        return True

    def shutdown(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            try: s.close()
            except Exception: pass


# 全局单例
_MANAGER: Optional[SshManager] = None
_MGR_LOCK = threading.Lock()


def get_manager() -> SshManager:
    global _MANAGER
    if _MANAGER is None:
        with _MGR_LOCK:
            if _MANAGER is None:
                _MANAGER = SshManager()
    return _MANAGER
