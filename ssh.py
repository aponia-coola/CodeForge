"""
SSH 远程会话管理 — 基于 asyncssh。

每个连接 = 一个 SshSession,所有 asyncssh 协程都提交到一个后台事件循环线程执行
(asyncssh 是 async,Flask 是 sync)。一个 manager 全局管理多 session。

主机指纹默认用 ~/.ssh/known_hosts 校验。未知主机不会直接连:connect() 抛
SshHostKeyError,.detail 里带一份待确认结构(含指纹和 confirm_token)。调用方
把指纹给用户核对,用户同意后调 SshManager.confirm_host_key(token) 写入
known_hosts(TOFU),然后重新发起一次 create()。指纹与记录不符时不提供 token,
必须人工处理 known_hosts。

公开 API:
    SshSession      — 单个连接(connect / exec / list_dir / read_file / write_file / close)
    SshManager      — 多 session 管理(create / get / list / destroy / shutdown
                      / confirm_host_key / cancel_host_key / reap_idle)
    SshHostKeyError — 指纹未通过校验(RuntimeError 子类),.detail 是待确认结构
    get_manager     — 取全局单例

环境变量:
    CODEFORGE_SSH_KNOWN_HOSTS    known_hosts 路径,缺省 ~/.ssh/known_hosts
    CODEFORGE_SSH_IDLE_TIMEOUT   会话空闲回收秒数,缺省 1800,范围 30~86400
    CODEFORGE_SSH_MAX_SESSIONS   最大并发会话数,缺省 8,范围 1~128
"""
from __future__ import annotations

import asyncio
import os
import shlex
import stat
import sys
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as _FutureTimeout
from contextlib import contextmanager
from typing import Any, Optional

import asyncssh


_DEFAULT_TIMEOUT  = 20
_CONNECT_TIMEOUT  = 15
_CLOSE_TIMEOUT    = 5
_HOST_KEY_TTL     = 300
_HOST_KEY_MAX     = 32
_REAP_INTERVAL    = 60


def _env_int(name: str, default: int, low: int, high: int) -> int:
    """读一个整数环境变量,缺省/非法回落到 default,并夹到 [low, high]。"""
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(low, min(high, val))


_IDLE_TIMEOUT = _env_int('CODEFORGE_SSH_IDLE_TIMEOUT', 1800, 30, 86400)
_MAX_SESSIONS = _env_int('CODEFORGE_SSH_MAX_SESSIONS', 8, 1, 128)


def _warn(msg: str) -> None:
    """把非致命错误写到 stderr。项目没有统一日志设施,这里只写不含凭据的信息。"""
    print(f'[ssh] {msg}', file=sys.stderr, flush=True)


def _err_text(e: BaseException) -> str:
    """异常转文本。只取类型名和 asyncssh/OS 自带的消息,不拼接任何凭据。"""
    return f'{type(e).__name__}: {e}'


# ════════════════════════════════════════════════════════════
#                    主机指纹与 known_hosts
# ════════════════════════════════════════════════════════════

class SshHostKeyError(RuntimeError):
    """
    主机指纹未通过校验,连接已被拒绝。

    .detail 是给调用方(main.py / 前端)用的待确认结构,字段固定:
        ok                 bool   恒为 False
        status             str    'host_key_unknown' | 'host_key_mismatch'
        host / port / user 目标信息
        key_type           str|None   服务器公钥算法,如 'ssh-ed25519'
        fingerprint        str|None   服务器公钥 SHA256 指纹,如 'SHA256:xxxx'
        fingerprint_md5    str|None   同一把公钥的 MD5 指纹
        known_fingerprints list[str]  known_hosts 里已记录的指纹
        known_hosts        str        known_hosts 文件路径
        confirm_token      str|None   仅 host_key_unknown 有;确认时回传
        expires_at         float      token 过期时间戳,无 token 时为 0
        expires_in         int        token 有效秒数,无 token 时为 0
        message            str        给人看的中文说明

    status 语义:
        host_key_unknown   首次见到这台主机。核对指纹后调
                           SshManager.confirm_host_key(confirm_token) 写入
                           known_hosts,再重新连接。
        host_key_mismatch  指纹和 known_hosts 里的记录不一致,可能是中间人。
                           不提供 token,必须人工核对并手动修 known_hosts。
    """
    def __init__(self, detail: dict):
        super().__init__(detail.get('message') or 'host key not verified')
        self.detail = detail

    @property
    def status(self) -> str:
        return self.detail.get('status') or 'host_key_unknown'


def _known_hosts_path() -> str:
    """known_hosts 路径。可用 CODEFORGE_SSH_KNOWN_HOSTS 覆盖。"""
    override = (os.environ.get('CODEFORGE_SSH_KNOWN_HOSTS') or '').strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser('~'), '.ssh', 'known_hosts')


def _host_pattern(host: str, port: int) -> str:
    """known_hosts 里的主机模式。非 22 端口用 OpenSSH 的 [host]:port 写法。"""
    return host if int(port) == 22 else f'[{host}]:{int(port)}'


def _match_known_hosts(path: str, host: str, port: int) -> list:
    """
    查 known_hosts 里已记录的、可信的公钥。走 asyncssh 自己的匹配逻辑,
    保证和 connect() 时的判定一致(支持通配符、hash 过的条目、[host]:port)。
    文件不存在按"没有记录"处理;文件存在但解析失败要抛出来,不能当成没记录。
    """
    if not os.path.isfile(path):
        return []
    try:
        matched = asyncssh.match_known_hosts(path, host, '', int(port))
    except OSError as e:
        raise RuntimeError(f'known_hosts 读取失败: {path}: {_err_text(e)}') from e
    except (ValueError, asyncssh.Error) as e:
        raise RuntimeError(f'known_hosts 解析失败: {path}: {_err_text(e)}') from e
    host_keys, ca_keys = list(matched[0]), list(matched[1])
    return host_keys + ca_keys


def _fingerprints(keys: list) -> list[str]:
    """一组公钥的 SHA256 指纹。"""
    out = []
    for k in keys:
        try:
            out.append(k.get_fingerprint('sha256'))
        except (ValueError, asyncssh.Error):
            continue
    return out


def _append_known_host(path: str, host: str, port: int, key) -> str:
    """
    把一把服务器公钥追加进 known_hosts,返回写入的整行。
    目录按 0700、文件按 0600 收权限(Windows 上 chmod 无效,静默跳过)。
    """
    line = _host_pattern(host, port) + ' ' + key.export_public_key('openssh').decode('ascii').strip()
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
        _chmod_quiet(folder, stat.S_IRWXU)
    need_newline = False
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        with open(path, 'rb') as f:
            f.seek(-1, os.SEEK_END)
            need_newline = f.read(1) != b'\n'
    with open(path, 'a', encoding='utf-8', newline='\n') as f:
        if need_newline:
            f.write('\n')
        f.write(line + '\n')
        f.flush()
        os.fsync(f.fileno())
    _chmod_quiet(path, stat.S_IRUSR | stat.S_IWUSR)
    return line


def _chmod_quiet(path: str, mode: int) -> None:
    """收权限。Windows 上 POSIX 权限位没意义,失败只告警不中断。"""
    if sys.platform.startswith('win'):
        return
    try:
        os.chmod(path, mode)
    except OSError as e:
        _warn(f'设置权限失败 {path}: {_err_text(e)}')


class _HostKeyGate:
    """
    未知主机指纹的待确认票据表。token -> 服务器公钥,带 TTL 和条数上限。
    只存公钥(公开信息),绝不存密码/私钥。
    """
    def __init__(self, ttl: int = _HOST_KEY_TTL, limit: int = _HOST_KEY_MAX):
        self._ttl     = ttl
        self._limit   = limit
        self._items: dict[str, dict] = {}
        self._lock    = threading.Lock()

    def offer(self, host: str, port: int, user: str, key, known_hosts: str,
              known_keys: list | None = None) -> dict:
        """登记一把待确认的公钥,返回待确认结构(含 confirm_token)。"""
        token = uuid.uuid4().hex
        now   = time.time()
        with self._lock:
            self._sweep(now)
            if len(self._items) >= self._limit:
                oldest = min(self._items, key=lambda t: self._items[t]['created_at'])
                self._items.pop(oldest, None)
            self._items[token] = {
                'host': host, 'port': int(port), 'key': key,
                'known_hosts': known_hosts,
                'created_at': now, 'expires_at': now + self._ttl,
            }
        return {
            'ok':                 False,
            'status':             'host_key_unknown',
            'host':               host,
            'port':               int(port),
            'user':               user,
            'key_type':           key.get_algorithm(),
            'fingerprint':        key.get_fingerprint('sha256'),
            'fingerprint_md5':    key.get_fingerprint('md5'),
            'known_fingerprints': _fingerprints(known_keys or []),
            'known_hosts':        known_hosts,
            'confirm_token':      token,
            'expires_at':         now + self._ttl,
            'expires_in':         self._ttl,
            'message': (f'{host}:{int(port)} 的主机指纹不在 known_hosts 中,连接已中止。'
                        f'请先用别的可信渠道核对指纹,确认无误后再接受;'
                        f'接受后会写入 {known_hosts}。'),
        }

    def take(self, token: str) -> Optional[dict]:
        """取出并删除一条票据。不存在或已过期返回 None。"""
        now = time.time()
        with self._lock:
            self._sweep(now)
            item = self._items.pop(token, None)
        if not item or item['expires_at'] < now:
            return None
        return item

    def drop(self, token: str) -> bool:
        """丢弃一条票据(用户拒绝)。"""
        with self._lock:
            return self._items.pop(token, None) is not None

    def _sweep(self, now: float) -> None:
        for t in [t for t, v in self._items.items() if v['expires_at'] < now]:
            self._items.pop(t, None)


_GATE = _HostKeyGate()


# ════════════════════════════════════════════════════════════
#                    后台事件循环线程
# ════════════════════════════════════════════════════════════

class _LoopThread:
    """
    一个独立线程,里面跑 asyncio 事件循环,给 asyncssh 用。

    线程安全要点:
    - loop 在父线程里创建好再交给子线程 run_forever,submit 的调用方不会读到 None
    - _ready 由循环内的第一个回调置位,置位时 loop 一定已经在跑
    - 跨线程提交协程用 submit(coro, timeout),跨线程调用 asyncssh 的同步方法
      (如 conn.close())必须用 call_sync,不能丢给 submit
    - 超时会取消掉循环里的任务,不留悬挂协程
    """
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = False
        self._lock = threading.Lock()
        self.start()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            loop = asyncio.new_event_loop()
            self.loop = loop
            self._stopped = False
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, args=(loop,),
                                            name='ssh-loop', daemon=True)
            self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError('ssh 事件循环线程启动失败')

    def _run(self, loop: asyncio.AbstractEventLoop):
        asyncio.set_event_loop(loop)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            try:
                self._drain(loop)
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    @staticmethod
    def _drain(loop: asyncio.AbstractEventLoop):
        """停循环前把还没跑完的任务取消掉,避免 close() 时报未完成任务。"""
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())

    def _check(self) -> asyncio.AbstractEventLoop:
        loop = self.loop
        if self._stopped or loop is None or loop.is_closed():
            raise RuntimeError('ssh 事件循环未运行')
        if self._thread is not None and threading.current_thread() is self._thread:
            raise RuntimeError('不能在 ssh 事件循环线程内同步等待自己')
        return loop

    def submit(self, coro, timeout: float = _DEFAULT_TIMEOUT) -> Any:
        """跨线程跑一个协程,同步等结果。超时取消任务并抛 TimeoutError。"""
        try:
            loop = self._check()
        except RuntimeError:
            coro.close()
            raise
        fut: Future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=timeout)
        except _FutureTimeout:
            # 协程自己超时和等待超时是同一个异常类型,用 done() 区分,别改写远端错误
            if fut.done():
                raise
            fut.cancel()
            raise TimeoutError(f'ssh 操作超时({timeout}s)') from None

    def call_sync(self, fn, timeout: float = _CLOSE_TIMEOUT) -> Any:
        """
        在事件循环线程里执行一个普通(非协程)函数并拿回结果。
        asyncssh 的 close()/exit() 都是同步方法,但会动 transport,必须回到
        循环线程里调,不能在 Flask 线程直接调,也不能丢给 submit。
        """
        loop = self._check()
        box: Future = Future()

        def _runner():
            try:
                box.set_result(fn())
            except BaseException as e:
                box.set_exception(e)

        loop.call_soon_threadsafe(_runner)
        try:
            return box.result(timeout=timeout)
        except _FutureTimeout:
            if box.done():
                raise
            raise TimeoutError(f'ssh 操作超时({timeout}s)') from None

    def close(self):
        """停掉事件循环。线程是 daemon,run_forever 返回后自行退出。"""
        with self._lock:
            if self._stopped or self.loop is None:
                return
            self._stopped = True
            loop = self.loop
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError as e:
            _warn(f'停止事件循环失败: {_err_text(e)}')


# ════════════════════════════════════════════════════════════
#                          SshSession
# ════════════════════════════════════════════════════════════

class SshSession:
    """
    单个 SSH 连接。线程安全,所有操作都是同步阻塞的。

    密码只存在 self._password 里,不进 info()、不进 repr、不进日志、不进异常消息。
    """
    def __init__(self, host: str, port: int, user: str,
                 password: str = '', key_path: str = '',
                 loop_thread: _LoopThread | None = None,
                 insecure: bool = False,
                 idle_timeout: int = _IDLE_TIMEOUT):
        self.id           = 'ssh_' + uuid.uuid4().hex[:10]
        self.host         = host
        self.port         = int(port or 22)
        self.user         = user or ''
        self.insecure     = bool(insecure)
        self.idle_timeout = int(idle_timeout)
        self.known_hosts  = _known_hosts_path()
        self._password    = password or ''
        self._key_path    = key_path or ''
        self._loop        = loop_thread or _LoopThread()
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._sftp: Optional[asyncssh.SFTPClient] = None
        self._sftp_lock   = threading.Lock()
        self._cmd_lock    = threading.Lock()
        self._state_lock  = threading.Lock()
        self._closed      = False
        self._busy        = 0
        self._host_key_verified = False
        self.created_at   = time.time()
        self._last_used   = self.created_at
        self._connect_error: str | None = None

    def __repr__(self) -> str:
        return f'<SshSession {self.id} {self.user}@{self.host}:{self.port}>'

    # ---------- 连接 ----------
    def connect(self, timeout: float = _CONNECT_TIMEOUT):
        """
        同步打开 SSH 连接。

        insecure=False(默认)时先校验主机指纹:
            known_hosts 里没有这台主机 -> 不连接,抛 SshHostKeyError
                                          (status=host_key_unknown,带 confirm_token)
            指纹和记录对不上           -> 不连接,抛 SshHostKeyError
                                          (status=host_key_mismatch,无 token)
        insecure=True 时跳过校验,info()['host_key_verified'] 为 False 并带 warning。
        其它失败抛 RuntimeError。
        """
        if self._closed:
            raise RuntimeError('会话已关闭')
        if self._conn is not None and not self._conn.is_closed():
            return

        client_keys = self._load_client_keys()
        known_arg: Any = None
        known_keys: list = []
        if self.insecure:
            _warn(f'{self.host}:{self.port} 以 insecure 模式连接,未校验主机指纹')
        else:
            known_keys = _match_known_hosts(self.known_hosts, self.host, self.port)
            if not known_keys:
                key = self._probe_host_key(timeout)
                raise SshHostKeyError(_GATE.offer(self.host, self.port, self.user,
                                                 key, self.known_hosts, known_keys))
            known_arg = self.known_hosts

        async def _do_connect():
            return await asyncio.wait_for(
                asyncssh.connect(
                    self.host,
                    port=self.port,
                    username=self.user,
                    password=self._password or None,
                    client_keys=client_keys,
                    known_hosts=known_arg,
                ),
                timeout=timeout,
            )

        try:
            conn = self._loop.submit(_do_connect(), timeout=timeout + 2)
        except asyncssh.HostKeyNotVerifiable as e:
            raise SshHostKeyError(self._mismatch_detail(known_keys, _err_text(e))) from e
        except asyncio.TimeoutError as e:
            raise RuntimeError(f'SSH 连接失败: 连接超时({timeout}s)') from e
        except Exception as e:
            self._connect_error = _err_text(e)
            raise RuntimeError(f'SSH 连接失败: {self._connect_error}') from e

        with self._state_lock:
            self._conn = conn
            self._host_key_verified = not self.insecure
            self._last_used = time.time()

    def _load_client_keys(self) -> Optional[list]:
        """
        读私钥。没配 key_path 就返回 None,让 asyncssh 走默认 ~/.ssh 里的键。
        配了但读不出来必须报错,不能静默退回密码认证。
        """
        if not self._key_path:
            return None
        if not os.path.isfile(self._key_path):
            raise RuntimeError(f'私钥文件不存在: {self._key_path}')
        try:
            return [asyncssh.read_private_key(self._key_path)]
        except (OSError, ValueError, asyncssh.Error) as first:
            if not self._password:
                raise RuntimeError(f'私钥不可用: {self._key_path}: {_err_text(first)}') from first
        try:
            return [asyncssh.read_private_key(self._key_path, passphrase=self._password)]
        except (OSError, ValueError, asyncssh.Error) as e:
            raise RuntimeError(f'私钥不可用: {self._key_path}: {_err_text(e)}') from e

    def _probe_host_key(self, timeout: float):
        """只做协议协商,取回服务器公钥用于给用户核对指纹,不认证也不发密码。"""
        async def _probe():
            return await asyncio.wait_for(
                asyncssh.get_server_host_key(self.host, port=self.port),
                timeout=timeout,
            )
        try:
            key = self._loop.submit(_probe(), timeout=timeout + 2)
        except Exception as e:
            raise RuntimeError(f'获取主机指纹失败: {_err_text(e)}') from e
        if key is None:
            raise RuntimeError(f'获取主机指纹失败: {self.host}:{self.port} 未返回主机公钥')
        return key

    def _mismatch_detail(self, known_keys: list, detail: str) -> dict:
        return {
            'ok':                 False,
            'status':             'host_key_mismatch',
            'host':               self.host,
            'port':               self.port,
            'user':               self.user,
            'key_type':           None,
            'fingerprint':        None,
            'fingerprint_md5':    None,
            'known_fingerprints': _fingerprints(known_keys),
            'known_hosts':        self.known_hosts,
            'confirm_token':      None,
            'expires_at':         0,
            'expires_in':         0,
            'message': (f'{self.host}:{self.port} 的主机指纹与 known_hosts 中的记录不一致,'
                        f'连接已中止。这可能是中间人攻击,也可能是服务器重装过。'
                        f'请人工核对后手动修改 {self.known_hosts},不提供一键接受。'),
            'detail':             detail,
        }

    def _ensure_sftp(self):
        if self._sftp is not None:
            return self._sftp
        with self._sftp_lock:
            if self._sftp is not None:
                return self._sftp
            conn = self._conn
            if conn is None or conn.is_closed():
                raise RuntimeError('not connected')

            async def _open():
                return await conn.start_sftp_client()
            self._sftp = self._loop.submit(_open(), timeout=_DEFAULT_TIMEOUT)
            return self._sftp

    # ---------- 生命周期 ----------
    @contextmanager
    def _activity(self):
        """标记一次活动:刷新空闲计时,并在执行期间挡住空闲回收。"""
        with self._state_lock:
            self._busy += 1
            self._last_used = time.time()
        try:
            yield
        finally:
            with self._state_lock:
                self._busy -= 1
                self._last_used = time.time()

    def alive(self) -> bool:
        """连接是否还活着。"""
        return (not self._closed) and self._conn is not None and not self._conn.is_closed()

    def idle_seconds(self, now: float | None = None) -> float:
        """空闲秒数。有操作在跑时恒为 0。"""
        with self._state_lock:
            if self._busy > 0:
                return 0.0
            return max(0.0, (now or time.time()) - self._last_used)

    def is_expired(self, now: float | None = None) -> bool:
        return self.idle_seconds(now) >= self.idle_timeout

    # ---------- 信息 ----------
    def info(self) -> dict:
        """会话信息。不含密码、私钥路径等凭据。"""
        now = time.time()
        return {
            'id':                self.id,
            'host':              self.host,
            'port':              self.port,
            'user':              self.user,
            'connected':         self.alive(),
            'closed':            self._closed,
            'created_at':        self.created_at,
            'last_used':         self._last_used,
            'idle_seconds':      round(self.idle_seconds(now), 3),
            'idle_timeout':      self.idle_timeout,
            'host_key_verified': self._host_key_verified,
            'insecure':          self.insecure,
            'known_hosts':       None if self.insecure else self.known_hosts,
            'warning':           '本次连接未校验主机指纹(insecure)' if self.insecure else None,
        }

    # ---------- 跑命令 ----------
    def exec(self, command: str, cwd: str = '', timeout: int = _DEFAULT_TIMEOUT) -> dict:
        """
        在远端跑一条命令,捕获 stdout/stderr/exit_code。
        cwd 用 shlex.quote 做 shell 引用(Python 的 !r 不是 shell 引用,会被注入)。
        失败不抛异常,统一回 {'ok': False, 'error': ..., 'error_type': ...}。
        """
        if self._closed:
            return {'ok': False, 'error': 'session closed', 'error_type': 'closed',
                    'command': command}
        conn = self._conn
        if conn is None or conn.is_closed():
            return {'ok': False, 'error': 'not connected', 'error_type': 'not_connected',
                    'command': command}
        full = command if not cwd else f'cd {shlex.quote(cwd)} 2>/dev/null && {command}'

        async def _do_exec():
            proc = await conn.create_process(full)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.close()
                try:
                    await asyncio.wait_for(proc.wait_closed(), timeout=3)
                except (asyncio.TimeoutError, OSError, asyncssh.Error) as e:
                    _warn(f'{self.id} 超时命令收尾失败: {_err_text(e)}')
                return {'ok': False, 'error': f'timeout ({timeout}s)', 'error_type': 'timeout',
                        'stdout': '', 'stderr': '', 'returncode': -1}
            return {
                'stdout':     out.decode('utf-8', 'replace') if isinstance(out, (bytes, bytearray)) else (out or ''),
                'stderr':     err.decode('utf-8', 'replace') if isinstance(err, (bytes, bytearray)) else (err or ''),
                'returncode': proc.exit_status if proc.exit_status is not None else 0,
            }

        with self._cmd_lock, self._activity():
            try:
                res = self._loop.submit(_do_exec(), timeout=timeout + 2)
            except TimeoutError as e:
                return {'ok': False, 'error': str(e), 'error_type': 'timeout', 'command': command}
            except Exception as e:
                return {'ok': False, 'error': _err_text(e), 'error_type': 'exec_failed',
                        'command': command}
        if res.get('error'):
            res['command'] = command
            return res
        res['ok']      = res.get('returncode', 0) == 0
        res['command'] = command
        res['stdout']  = (res.get('stdout') or '').rstrip('\n')
        res['stderr']  = (res.get('stderr') or '').rstrip('\n')
        return res

    # ---------- SFTP 文件操作 ----------
    def list_dir(self, path: str) -> dict:
        """列目录。返回 [{name, type, size, mtime, path}]"""
        try:
            with self._activity():
                sftp = self._ensure_sftp()

                async def _resolve_and_list():
                    rp = await sftp.realpath(path) if path else (await sftp.getcwd() or '.')
                    return await _async_list(sftp, rp)
                real_items = self._loop.submit(_resolve_and_list(), timeout=_DEFAULT_TIMEOUT)
        except Exception as e:
            return {'ok': False, 'error': _err_text(e), 'path': path}
        return {
            'ok':    True,
            'path':  path,
            'items': real_items,
        }

    def read_file(self, path: str, max_bytes: int = 2 * 1024 * 1024) -> dict:
        try:
            with self._activity():
                sftp = self._ensure_sftp()
                data = self._loop.submit(_async_read(sftp, path, max_bytes), timeout=_DEFAULT_TIMEOUT)
        except Exception as e:
            return {'ok': False, 'error': _err_text(e), 'path': path}
        return {
            'ok':     True,
            'path':   path,
            'size':   len(data),
            'binary': False,
            'text':   data.decode('utf-8', 'replace'),
            'b64':    None,
        }

    def write_file(self, path: str, content: str) -> dict:
        data = content.encode('utf-8', 'replace')
        try:
            with self._activity():
                sftp = self._ensure_sftp()
                self._loop.submit(_async_write(sftp, path, data), timeout=_DEFAULT_TIMEOUT)
        except Exception as e:
            return {'ok': False, 'error': _err_text(e), 'path': path}
        return {'ok': True, 'path': path, 'size': len(data)}

    # ---------- 关闭 ----------
    def close(self, timeout: float = _CLOSE_TIMEOUT):
        """
        关闭 SFTP 和连接。

        asyncssh 的 close() 是同步方法,不是协程,必须用 call_sync 回到事件循环
        线程里调;之前丢给 run_coroutine_threadsafe 会直接 TypeError,再被吞掉,
        结果是连接根本没关。这里失败一律抛 RuntimeError,由调用方决定怎么处理。
        """
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            conn, self._conn = self._conn, None
            sftp, self._sftp = self._sftp, None
        errors = []
        if sftp is not None:
            try:
                self._loop.call_sync(sftp.exit, timeout=timeout)
                self._loop.submit(sftp.wait_closed(), timeout=timeout)
            except Exception as e:
                errors.append(f'sftp: {_err_text(e)}')
        if conn is not None:
            try:
                self._loop.call_sync(conn.close, timeout=timeout)
                self._loop.submit(conn.wait_closed(), timeout=timeout)
            except Exception as e:
                errors.append(f'conn: {_err_text(e)}')
        if errors:
            raise RuntimeError(f'SSH 关闭失败 {self.id}: ' + '; '.join(errors))


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
    async with sftp.open(path, 'wb') as f:
        await f.write(data)


# ════════════════════════════════════════════════════════════
#                          SshManager
# ════════════════════════════════════════════════════════════

class SshManager:
    """
    多 SSH 会话管理。所有 session 共用一个事件循环线程。

    会话有两道生命周期约束,防止连接永久泄漏:
    - 空闲超过 idle_timeout 秒的会话由后台线程自动回收(有操作在跑时不回收)
    - 会话总数上限 max_sessions,满了 create() 直接拒绝
    """
    def __init__(self, idle_timeout: int = _IDLE_TIMEOUT,
                 max_sessions: int = _MAX_SESSIONS,
                 reap_interval: int = _REAP_INTERVAL):
        self._sessions: dict[str, SshSession] = {}
        self._lock          = threading.Lock()
        self._loop          = _LoopThread()
        self.idle_timeout   = int(idle_timeout)
        self.max_sessions   = int(max_sessions)
        self._reap_interval = int(reap_interval)
        self._stop          = threading.Event()
        self._reaper = threading.Thread(target=self._reap_forever,
                                        name='ssh-reaper', daemon=True)
        self._reaper.start()

    # ---------- 会话 ----------
    def create(self, host: str, port: int, user: str,
               password: str = '', key_path: str = '',
               insecure: bool = False,
               timeout: float = _CONNECT_TIMEOUT) -> SshSession:
        """
        建立一个新会话。成功返回 SshSession。
        主机指纹未知/不符抛 SshHostKeyError(RuntimeError 子类,.detail 是待确认结构)。
        其它失败抛 RuntimeError。insecure=True 跳过指纹校验,默认关闭。
        """
        self.reap_idle()
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError(f'SSH 会话数已达上限({self.max_sessions}),'
                                   f'请先断开不用的会话')
        s = SshSession(host, port, user, password=password, key_path=key_path,
                       loop_thread=self._loop, insecure=insecure,
                       idle_timeout=self.idle_timeout)
        s.connect(timeout=timeout)
        with self._lock:
            over_limit = len(self._sessions) >= self.max_sessions
            if not over_limit:
                self._sessions[s.id] = s
        if over_limit:
            self._close_quiet(s)
            raise RuntimeError(f'SSH 会话数已达上限({self.max_sessions}),'
                               f'请先断开不用的会话')
        return s

    def get(self, sid: str) -> Optional[SshSession]:
        with self._lock:
            return self._sessions.get(sid)

    def list(self) -> list[dict]:
        with self._lock:
            return [s.info() for s in self._sessions.values()]

    def destroy(self, sid: str) -> bool:
        """关闭并移除会话。关闭失败抛 RuntimeError(会话仍会从表里摘掉)。"""
        with self._lock:
            s = self._sessions.pop(sid, None)
        if not s:
            return False
        s.close()
        return True

    def shutdown(self):
        """关掉所有会话和事件循环。批量操作,单个会话的关闭异常只告警。"""
        self._stop.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            self._close_quiet(s)
        self._loop.close()

    # ---------- 空闲回收 ----------
    def reap_idle(self) -> list[str]:
        """回收空闲超时或已断线的会话,返回被回收的 sid。"""
        now = time.time()
        dead: list[SshSession] = []
        with self._lock:
            for sid, s in list(self._sessions.items()):
                if s.is_expired(now) or not s.alive():
                    self._sessions.pop(sid, None)
                    dead.append(s)
        for s in dead:
            self._close_quiet(s)
        return [s.id for s in dead]

    def _reap_forever(self):
        while not self._stop.wait(self._reap_interval):
            try:
                reaped = self.reap_idle()
            except Exception as e:
                _warn(f'空闲回收异常: {_err_text(e)}')
                continue
            if reaped:
                _warn(f'回收空闲/断线会话: {", ".join(reaped)}')

    @staticmethod
    def _close_quiet(s: SshSession):
        """批量场景下的关闭:异常只告警,不影响其它会话。"""
        try:
            s.close()
        except Exception as e:
            _warn(_err_text(e))

    # ---------- 主机指纹确认 ----------
    def confirm_host_key(self, token: str) -> dict:
        """
        用户核对指纹后接受:把公钥写进 known_hosts(TOFU)。
        返回 {'ok': True, 'host', 'port', 'fingerprint', 'known_hosts', 'added'};
        token 不存在或已过期返回 {'ok': False, 'error': ...}。
        写入后需要调用方重新发起一次 create()。
        """
        item = _GATE.take(token)
        if not item:
            return {'ok': False, 'error': '确认票据不存在或已过期,请重新发起连接'}
        host, port, key = item['host'], item['port'], item['key']
        path = item['known_hosts']
        try:
            if _match_known_hosts(path, host, port):
                added, line = False, ''
            else:
                line  = _append_known_host(path, host, port, key)
                added = True
        except OSError as e:
            return {'ok': False, 'error': f'写入 known_hosts 失败: {path}: {_err_text(e)}'}
        except RuntimeError as e:
            return {'ok': False, 'error': str(e)}
        return {
            'ok':          True,
            'host':        host,
            'port':        port,
            'key_type':    key.get_algorithm(),
            'fingerprint': key.get_fingerprint('sha256'),
            'known_hosts': path,
            'added':       added,
            'line':        line,
        }

    def cancel_host_key(self, token: str) -> dict:
        """用户拒绝:丢弃待确认票据。"""
        return {'ok': _GATE.drop(token)}


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
