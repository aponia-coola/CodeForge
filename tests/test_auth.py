"""
auth.py 与 HTTP 认证关卡的测试。

这些接口能执行任意命令、读写任意文件,所以三道关卡任何一道漏掉都是本地提权:
Host 白名单挡 DNS 重绑定,Origin 同源挡跨站请求,token 挡未授权访问。
"""
import pytest

import auth


TOKEN_HEADER = auth.TOKEN_HEADER
TOKEN = auth.token()


# ════════════════════════════════════════════════════════════
#                          token
# ════════════════════════════════════════════════════════════

def test_missing_token_is_rejected(client):
    """不带 token 的 API 请求必须 401。"""
    resp = client.get("/api/config")
    assert resp.status_code == 401
    assert resp.get_json() == {"ok": False, "error": "unauthorized"}


def test_wrong_token_is_rejected(client):
    """token 错误必须 401。"""
    resp = client.get("/api/config", headers={TOKEN_HEADER: "wrong-token"})
    assert resp.status_code == 401


def test_empty_token_header_is_rejected(client):
    """token 头存在但为空值同样必须 401。"""
    assert client.get("/api/config", headers={TOKEN_HEADER: ""}).status_code == 401


def test_correct_token_passes(client):
    """正确的 token 应当放行。"""
    resp = client.get("/api/config", headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_authed_client_passes(authed_client):
    """authed_client 夹具自带的头应当直接可用。"""
    assert authed_client.get("/api/config").status_code == 200


def test_mutating_route_requires_token(client):
    """能执行命令的路由裸奔时必须 401,而不是先执行再校验。"""
    resp = client.post("/api/terminal/run", json={"command": "echo pwned"})
    assert resp.status_code == 401


def test_check_token_is_constant_time_comparison():
    """token 比较必须走 hmac.compare_digest,不接受前缀匹配。"""
    assert auth.check_token(TOKEN) is True
    assert auth.check_token(TOKEN[:-1]) is False
    assert auth.check_token(TOKEN + "x") is False
    assert auth.check_token(None) is False


def test_check_token_strips_whitespace():
    """请求头里常见的首尾空白应当被容忍。"""
    assert auth.check_token(f"  {TOKEN}  ") is True


# ════════════════════════════════════════════════════════════
#                          豁免路由
# ════════════════════════════════════════════════════════════

def test_index_is_exempt(client):
    """GET / 是唯一豁免 token 的页面。"""
    assert client.get("/").status_code == 200


def test_static_get_is_exempt(client):
    """GET /static/* 豁免 token,否则页面自己都加载不了。"""
    resp = client.get("/static/app.js")
    assert resp.status_code == 200


def test_static_post_is_not_exempt(client):
    """豁免只针对 GET / HEAD,POST /static/* 仍然要 token。"""
    assert client.post("/static/app.js").status_code == 401


def test_is_exempt_matrix():
    """豁免判定的边界:方法与路径前缀都要对上才算豁免。"""
    assert auth.is_exempt("/", "GET") is True
    assert auth.is_exempt("/", "HEAD") is True
    assert auth.is_exempt("/", "POST") is False
    assert auth.is_exempt("/static/app.js", "GET") is True
    assert auth.is_exempt("/static/app.js", "POST") is False
    assert auth.is_exempt("/staticfake/x", "GET") is False
    assert auth.is_exempt("/api/config", "GET") is False


def test_api_options_is_not_exempt(client):
    """OPTIONS 不豁免:同源请求不会预检,跨源请求本来就该拒。"""
    assert client.options("/api/config").status_code == 401


# ════════════════════════════════════════════════════════════
#                        Host 白名单
# ════════════════════════════════════════════════════════════

@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "192.168.1.7", "10.0.0.5"])
def test_loopback_and_ip_hosts_are_allowed(client, host):
    """回环名与 IP 字面量放行:IP 无法被 DNS 重绑定,局域网访问不受影响。"""
    resp = client.get("/api/config", headers={TOKEN_HEADER: TOKEN, "Host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize("host", ["evil.com", "attacker.example.org", "localhost.evil.com", ""])
def test_rebinding_hosts_are_rejected(client, host):
    """带域名的 Host 一律拒绝,这是 DNS 重绑定攻击的必经之路。"""
    resp = client.get("/api/config", headers={TOKEN_HEADER: TOKEN, "Host": host})
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "forbidden_host"


def test_bad_host_is_rejected_before_token(client):
    """Host 校验在 token 之前,坏 Host 即使没带 token 也应当是 403 而不是 401。"""
    assert client.get("/api/config", headers={"Host": "evil.com"}).status_code == 403


def test_bad_host_blocks_even_exempt_routes(client):
    """豁免只免 token,不免 Host 校验。"""
    assert client.get("/", headers={"Host": "evil.com"}).status_code == 403


def test_host_allowed_unit():
    """host_allowed 的直接单测,覆盖各种写法。"""
    assert auth.host_allowed("localhost") is True
    assert auth.host_allowed("127.0.0.1:9191") is True
    assert auth.host_allowed("[::1]:9191") is True
    assert auth.host_allowed("evil.com") is False
    assert auth.host_allowed("evil.com:9191") is False
    assert auth.host_allowed("") is False
    assert auth.host_allowed("127.0.0.1:notaport") is False


def test_host_port_must_match_when_configured(monkeypatch):
    """配置了服务端口后,Host 里的端口必须一致。"""
    monkeypatch.setattr(auth, "_PORT", 9191)
    assert auth.host_allowed("127.0.0.1:9191") is True
    assert auth.host_allowed("127.0.0.1:9999") is False
    assert auth.host_allowed("127.0.0.1") is True


# ════════════════════════════════════════════════════════════
#                        Origin 同源
# ════════════════════════════════════════════════════════════

def test_cross_origin_is_rejected(client):
    """Origin 与 Host 不一致的请求必须 403。"""
    resp = client.get(
        "/api/config",
        headers={TOKEN_HEADER: TOKEN, "Host": "127.0.0.1", "Origin": "http://evil.com"},
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "forbidden_origin"


def test_null_origin_is_rejected(client):
    """Origin: null 来自沙箱化的 iframe 或 data: 页面,一律拒绝。"""
    resp = client.get(
        "/api/config",
        headers={TOKEN_HEADER: TOKEN, "Host": "127.0.0.1", "Origin": "null"},
    )
    assert resp.status_code == 403


def test_same_origin_passes(client):
    """Origin 与 Host 一致时放行。"""
    resp = client.get(
        "/api/config",
        headers={TOKEN_HEADER: TOKEN, "Host": "127.0.0.1", "Origin": "http://127.0.0.1"},
    )
    assert resp.status_code == 200


def test_no_origin_is_treated_as_same_origin(client):
    """没有 Origin 头视为同源,普通的 fetch 与地址栏访问都不带它。"""
    assert client.get("/api/config", headers={TOKEN_HEADER: TOKEN}).status_code == 200


def test_origin_allowed_unit():
    """origin_allowed 的直接单测。"""
    assert auth.origin_allowed("", "127.0.0.1") is True
    assert auth.origin_allowed("http://127.0.0.1", "127.0.0.1") is True
    assert auth.origin_allowed("https://127.0.0.1", "127.0.0.1") is True
    assert auth.origin_allowed("http://evil.com", "127.0.0.1") is False
    assert auth.origin_allowed("null", "127.0.0.1") is False
    assert auth.origin_allowed("file://", "127.0.0.1") is False
    assert auth.origin_allowed("http://127.0.0.1:9191", "127.0.0.1") is False


# ════════════════════════════════════════════════════════════
#                          会话标识
# ════════════════════════════════════════════════════════════

class _FakeRequest:
    """只带 headers 的请求替身,用于直接单测 session_id。"""

    def __init__(self, headers):
        """只保留 headers,session_id 用不到别的字段。"""
        self.headers = headers


def _sid(value):
    """用给定的头值跑一次 session_id。"""
    return auth.session_id(_FakeRequest({auth.SESSION_HEADER: value} if value is not None else {}))


def test_session_id_accepts_normal_value():
    """合法 sid 原样返回。"""
    assert _sid("tab-1_abc") == "tab-1_abc"


def test_session_id_rejects_illegal_characters():
    """非法字符的 sid 落回 default,避免任意请求头在会话表里开新槽。"""
    for bad in ["../evil", "a b", "sid;drop", "中文", "x" * 65]:
        assert _sid(bad) is None


def test_session_id_missing_is_none():
    """缺失或空白的 sid 返回 None。"""
    assert _sid(None) is None
    assert _sid("   ") is None
