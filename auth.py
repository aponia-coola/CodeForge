"""
HTTP 认证与来源校验。

CodeForge 的接口能执行任意命令、读写任意文件,任何能连上端口的设备都等同于拿到
本机 shell,所以不能裸奔。本模块提供三道关卡,由 install() 挂到 before_request:

1. Host 白名单:只接受回环名、IP 字面量和显式配置的主机名。
   攻击者的网页可以用短 TTL 的 DNS 把自己的域名重绑定到 127.0.0.1,浏览器此后
   认为同源,CORS 不再拦截 —— 但重绑定必须依赖域名,Host 头会带上攻击者的域名,
   在这里就被拒掉。IP 字面量无法被重绑定,所以放行,局域网按 IP 访问不受影响。
2. Origin 同源:带 Origin 且与 Host 不一致的请求直接拒绝。
3. token:除 GET / 与 /static/* 外,全部要求 X-CodeForge-Token,用
   hmac.compare_digest 比较,避免逐字节比较的时序侧信道。

约定:
    请求头 X-CodeForge-Token:   <token>,除豁免路由外必需
    请求头 X-CodeForge-Session: <sid>,可选,缺省落到 default 会话
    token 来源 CODEFORGE_TOKEN 环境变量,未设置则启动时随机生成并打印在 banner
    额外放行的主机名用 CODEFORGE_ALLOWED_HOSTS 配置(逗号分隔)
"""
import hmac
import ipaddress
import os
import secrets
from urllib.parse import urlsplit

from flask import jsonify, request as flask_request

TOKEN_HEADER   = 'X-CodeForge-Token'
SESSION_HEADER = 'X-CodeForge-Session'
TOKEN_ENV      = 'CODEFORGE_TOKEN'
HOSTS_ENV      = 'CODEFORGE_ALLOWED_HOSTS'

MAX_SID_LEN = 64
_SID_CHARS  = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_')

_LOOPBACK_NAMES = frozenset({'localhost', 'localhost.localdomain', 'ip6-localhost'})
_EXEMPT_EXACT   = frozenset({'/', '/favicon.ico'})
_EXEMPT_PREFIX  = ('/static/',)

_TOKEN = ''
_TOKEN_SOURCE = ''
_ALLOWED_HOSTS: set[str] = set()
_PORT: int | None = None


# ════════════════════════════════════════════════════════════
#                          token
# ════════════════════════════════════════════════════════════

def token() -> str:
    """取当前 token,首次调用时从环境变量读取或随机生成。"""
    global _TOKEN, _TOKEN_SOURCE
    if not _TOKEN:
        env = (os.environ.get(TOKEN_ENV) or '').strip()
        if env:
            _TOKEN, _TOKEN_SOURCE = env, 'env'
        else:
            _TOKEN, _TOKEN_SOURCE = secrets.token_urlsafe(32), 'generated'
    return _TOKEN


def token_source() -> str:
    """token 的来源:env(环境变量)或 generated(本次启动随机生成)。"""
    token()
    return _TOKEN_SOURCE


def check_token(value: str | None) -> bool:
    """恒定时间比较请求带来的 token。"""
    return hmac.compare_digest((value or '').strip(), token())


# ════════════════════════════════════════════════════════════
#                        Host / Origin
# ════════════════════════════════════════════════════════════

def configure(port: int | None = None, extra_hosts=()) -> None:
    """启动时登记服务端口与额外放行的主机名,供 Host 校验使用。"""
    global _PORT
    if port is not None:
        _PORT = int(port)
    for item in list(extra_hosts) + _env_hosts():
        name, _, bad = _split_hostport(item)
        if not bad and name:
            _ALLOWED_HOSTS.add(name)


def _env_hosts() -> list[str]:
    """从 CODEFORGE_ALLOWED_HOSTS 读逗号分隔的主机名列表。"""
    raw = os.environ.get(HOSTS_ENV) or ''
    return [s.strip() for s in raw.split(',') if s.strip()]


def allowed_hosts() -> list[str]:
    """当前显式放行的主机名(不含回环名和 IP 字面量这两条通用规则)。"""
    return sorted(_ALLOWED_HOSTS)


def _split_hostport(raw: str):
    """
    拆 Host 头为 (主机, 端口, 是否非法)。
    支持 host、host:port、[v6]、[v6]:port 四种写法,端口缺省时返回 None。
    """
    h = (raw or '').strip()
    if not h:
        return '', None, True
    if h.startswith('['):
        end = h.find(']')
        if end < 0:
            return '', None, True
        name, rest = h[1:end], h[end + 1:]
        if rest and not rest.startswith(':'):
            return '', None, True
        port_s = rest[1:] if rest else ''
    elif h.count(':') == 1:
        name, _, port_s = h.partition(':')
    elif ':' in h:
        name, port_s = h, ''
    else:
        name, port_s = h, ''
    name = name.strip().rstrip('.').lower()
    if not name:
        return '', None, True
    if not port_s:
        return name, None, False
    if not port_s.isdigit():
        return '', None, True
    return name, int(port_s), False


def _is_ip_literal(name: str) -> bool:
    """主机名是否是 IP 字面量(IP 无法被 DNS 重绑定,可以放行)。"""
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def host_allowed(raw_host: str) -> bool:
    """校验 Host 头:端口须与服务端口一致,主机名须是回环名/IP/显式白名单。"""
    name, port, bad = _split_hostport(raw_host)
    if bad:
        return False
    if _PORT is not None and port is not None and port != _PORT:
        return False
    if name in _LOOPBACK_NAMES or name in _ALLOWED_HOSTS:
        return True
    return _is_ip_literal(name)


def origin_allowed(origin: str, raw_host: str) -> bool:
    """没有 Origin 视为同源;有 Origin 时必须与 Host 完全一致。"""
    o = (origin or '').strip()
    if not o:
        return True
    if o == 'null':
        return False
    parts = urlsplit(o)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return False
    return parts.netloc.strip().rstrip('.').lower() == (raw_host or '').strip().rstrip('.').lower()


# ════════════════════════════════════════════════════════════
#                        会话与豁免
# ════════════════════════════════════════════════════════════

def session_id(req=None) -> str | None:
    """
    取 X-CodeForge-Session。
    只接受 [A-Za-z0-9-_] 且不超过 64 字符,非法或缺失时返回 None(落到 default 会话),
    避免任意请求头内容在会话表里无限开新槽。
    """
    r = req if req is not None else flask_request
    sid = (r.headers.get(SESSION_HEADER) or '').strip()
    if not sid or len(sid) > MAX_SID_LEN:
        return None
    if any(c not in _SID_CHARS for c in sid):
        return None
    return sid


def is_exempt(path: str, method: str) -> bool:
    """页面本身和静态资源不要求 token,其余一律要求。"""
    if method not in ('GET', 'HEAD'):
        return False
    return path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIX)


def check_request(req=None):
    """
    校验一个请求。通过返回 None,不通过返回 (JSON 载荷, HTTP 状态码)。
    """
    r = req if req is not None else flask_request
    raw_host = r.headers.get('Host', '')
    if not host_allowed(raw_host):
        return {
            'ok': False,
            'error': 'forbidden_host',
            'detail': f'Host 不在允许范围内: {raw_host}',
        }, 403
    if not origin_allowed(r.headers.get('Origin', ''), raw_host):
        return {
            'ok': False,
            'error': 'forbidden_origin',
            'detail': '跨源请求已被拒绝',
        }, 403
    if is_exempt(r.path, r.method):
        return None
    if not check_token(r.headers.get(TOKEN_HEADER)):
        return {'ok': False, 'error': 'unauthorized'}, 401
    return None


def install(app) -> None:
    """把三道关卡挂到 Flask 的 before_request 上。"""
    @app.before_request
    def _guard():
        """每个请求进入视图之前先过 Host / Origin / token 三道校验。"""
        bad = check_request(flask_request)
        if bad is None:
            return None
        payload, status = bad
        return jsonify(payload), status


# ════════════════════════════════════════════════════════════
#                          启动横幅
# ════════════════════════════════════════════════════════════

def display_host(host: str) -> str:
    """把通配绑定地址换成可点击的回环地址。"""
    if host in ('0.0.0.0', '::', ''):
        return '127.0.0.1'
    if ':' in host and not host.startswith('['):
        return f'[{host}]'
    return host


def is_loopback_bind(host: str) -> bool:
    """绑定地址是否只暴露给本机。"""
    h = (host or '').strip().strip('[]')
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def banner(host: str, port: int) -> str:
    """
    启动横幅:带 token 的完整 URL,以及绑定到非回环地址时的风险提示。
    正文只用 ASCII 与 GBK 可编码的字符 —— 中文 Windows 控制台默认 cp936,
    横幅里出现 U+26A0 这类字符会让 print 抛 UnicodeEncodeError,服务在 app.run
    之前就死掉,而这恰好只发生在 --host 0.0.0.0 这条最需要看到警告的路径上。
    """
    url = f'http://{display_host(host)}:{port}/#token={token()}'
    src = '环境变量 CODEFORGE_TOKEN' if token_source() == 'env' else '本次启动随机生成'
    line = '═' * 68
    out = [
        line,
        '  CodeForge',
        f'  访问地址: {url}',
        f'  token   : {token()}',
        f'  来源    : {src}',
        f'  请求头  : {TOKEN_HEADER}: <token>',
        f'            {SESSION_HEADER}: <sid>(可选,缺省 default 会话)',
    ]
    if not is_loopback_bind(host):
        out += [
            line,
            f'  [!] 正在绑定非回环地址 {host},局域网内任意设备都能连到本服务。',
            '  [!] token 是此时唯一的防线:它一旦泄露,对方即可执行任意命令、读写任意文件。',
            '  [!] 只在受信任的网络里这么做,否则请改用 --host 127.0.0.1。',
        ]
    out.append(line)
    return '\n'.join(out)
