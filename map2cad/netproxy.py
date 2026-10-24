"""网络出口探测：自动找出本机可用的代理。

为什么需要这个：
  「速博士」「Clash」「V2RayN」这类软件开启全局/系统代理时，
  代理地址写在 Windows 注册表的 Internet Settings 里，**不会**出现在环境变量 http_proxy 中。
  而 Python 的 urllib 默认只认环境变量，所以会一直走直连 —— 直连到不了
  OpenStreetMap，就会看到 504 / 连接超时。

  这里把三条路都走一遍：
    1. 环境变量
    2. Windows 注册表（系统代理，最常命中）
    3. 扫描常见本地端口（TUN 模式 / 只监听本地的情况）
"""
import os
import socket
import time
import urllib.parse

# 常见代理软件默认端口（Clash 7890、V2RayN 10809、速博士系常在这几个里面）
COMMON_PORTS = [
    7890, 7897, 7891, 7892, 7893, 10809, 10808, 1080, 1087, 1081,
    2080, 2081, 8889, 8090, 8118, 33210, 20171, 6152, 6153, 9090, 8888,
]

_scan_cache = {"t": 0.0, "ports": []}
SCAN_TTL = 30.0


def normalize(p):
    """把 127.0.0.1:7890 之类补成 http://127.0.0.1:7890。"""
    p = str(p or "").strip()
    if not p:
        return None
    if "://" not in p:
        p = "http://" + p
    return p


def _from_env():
    out = []
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
              "all_proxy", "ALL_PROXY"):
        v = normalize(os.environ.get(k))
        if v:
            out.append(v)
    return out


def _parse_winproxy(server):
    """支持 '127.0.0.1:7890' 和 'http=1.2.3.4:80;https=1.2.3.4:443' 两种写法。"""
    server = str(server or "").strip()
    if not server:
        return []
    if "=" not in server:
        return ["http://" + server]
    parts = {}
    for seg in server.split(";"):
        if "=" not in seg:
            continue
        proto, host = seg.split("=", 1)
        proto, host = proto.strip().lower(), host.strip()
        if not host or host.startswith("<"):
            continue
        parts[proto] = host
    out = []
    for proto in ("https", "http", "socks"):
        if proto in parts:
            scheme = "socks5" if proto == "socks" else "http"
            out.append(f"{scheme}://{parts[proto]}")
    return out


def _from_registry(strict=True):
    """读 Windows「Internet 选项 → 局域网设置」里的代理（全局模式写在这儿）。

    strict=False 时不管 ProxyEnable 开关，只要记录过地址就返回 —— 因为不少
    加速器/代理软件退出后会把开关复位成 0，但端口其实还在，这只是个线索，
    真不真用得上由后面的探测决定。
    """
    try:
        import winreg
    except ImportError:
        return []
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
    except OSError:
        return []
    try:
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except OSError:
            enable = 0
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except OSError:
            server = ""
        if not server:
            return []
        if strict and not enable:
            return []
        return _parse_winproxy(server)
    except OSError:
        return []
    finally:
        try:
            winreg.CloseKey(key)
        except OSError:
            pass


def _listening_ports():
    """列出本机 127.0.0.1 上正在监听的端口（靠 netstat，比写死端口表靠谱）。

    代理软件爱用什么端口都有可能（速博士就用了 10090），死守一份清单迟早漏。
    """
    import re
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, timeout=6,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.stdout.decode("gbk", "replace")
    except Exception:  # noqa: BLE001
        return []
    ports = []
    for line in text.splitlines():
        m = re.search(r"(127\.0\.0\.1|0\.0\.0\.0|\[::1\]):(\d+)\s+\S+\s+LISTENING", line, re.I)
        if m:
            p = int(m.group(2))
            if p not in ports:
                ports.append(p)
    return ports


def _scan_local_ports(ports=None, timeout=0.25, use_cache=True):
    """并发扫一遍本机端口，返回正在监听的端口。

    候选 = 常见代理端口 + netstat 里实际在监听的端口，靠探测筛出真代理。
    """
    now = time.time()
    if use_cache and now - _scan_cache["t"] < SCAN_TTL and _scan_cache["ports"]:
        return list(_scan_cache["ports"])

    cand = list(ports or COMMON_PORTS)
    for p in _listening_ports():
        if p not in cand:
            cand.append(p)

    from concurrent.futures import ThreadPoolExecutor

    def probe(p):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                if s.connect_ex(("127.0.0.1", p)) == 0:
                    return p
        except Exception:  # noqa: BLE001
            pass
        return None

    alive = []
    with ThreadPoolExecutor(max_workers=min(64, len(cand))) as ex:
        for r in ex.map(probe, cand):
            if r:
                alive.append(r)
    # 常见代理端口排前面，其余按端口号
    alive.sort(key=lambda p: (COMMON_PORTS.index(p) if p in COMMON_PORTS else 99, p))
    _scan_cache["t"] = now
    _scan_cache["ports"] = alive
    return alive


def _looks_like_proxy(port, timeout=2.0):
    """发一个 CONNECT 探针，确认这个端口真的是 HTTP 代理，而不是随便一个服务。

    代理会回 200（能转发）或 407（要密码）；普通 HTTP 服务会回 400/404/405 之类。
    这样能把本机别的程序（下载器、模型转发器…）排除掉，界面提示不会被误报。
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"CONNECT www.example.com:443 HTTP/1.1\r\n"
                      b"Host: www.example.com:443\r\n\r\n")
            data = s.recv(200)
    except Exception:  # noqa: BLE001
        return False
    head = data[:24]
    return head.startswith(b"HTTP/1.") and (b" 200" in head or b" 407" in head)


def proxy_tunnel_ok(proxy_url, target="www.example.com:443", timeout=3.0):
    """这个代理能不能真的把流量转发出去？

    只测「端口在监听」是不够的：加速器经常端口开着、却根本不转发
    （或者只加速特定进程），这时候握手能连上、请求却全部超时。
    这里发一个 CONNECT，只有回 200 才算真能转发。
    """
    try:
        u = urllib.parse.urlsplit(normalize(proxy_url) or "")
        host, port = u.hostname, u.port or 8080
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            data = s.recv(200)
        head = data[:32]
        return head.startswith(b"HTTP/1.") and b" 200" in head
    except Exception:  # noqa: BLE001
        return False


def detect_proxy_ports(limit=4, scan_limit=40):
    """本机真正像 HTTP 代理的端口。

    注意要**全部**探一遍再挑，不能只看前几个 —— 加速器的端口号很随机
    （速博士用的 10090 排在十几个系统端口后面），只看前几个必然漏。
    """
    from concurrent.futures import ThreadPoolExecutor

    ports = _scan_local_ports()[:scan_limit]
    if not ports:
        return []
    with ThreadPoolExecutor(max_workers=min(32, len(ports))) as ex:
        flags = list(ex.map(_looks_like_proxy, ports))
    good = [p for p, ok in zip(ports, flags) if ok]
    return good[:limit]


def build_outlets(user_proxy=None, scan=True):
    """返回 [(说明, 代理地址或 None)]，已经按优先级排好。

    None 表示直连。
    """
    # 调试/自动化测试用：只走指定的一个出口，不自动发现
    only = os.environ.get("MAP2CAD_ONLY_OUTLET")
    if only:
        return [("测试指定出口", normalize(only))]

    outlets = []
    seen = set()

    def add(label, url):
        key = (url or "direct").lower()
        # socks5 走不了 urllib，统一按 http 试一次；同一个端口只留一个
        key = key.replace("socks5://", "http://")
        if key in seen:
            return
        seen.add(key)
        outlets.append((label, url))

    if user_proxy:
        add("手动填写的代理", normalize(user_proxy))
    for u in _from_env():
        add("环境变量代理", u)
    for u in _from_registry():
        add("系统代理(注册表)", u)
    if scan:
        # 注册表开关被复位（加速器退出常见）也留个线索，能不能用交给探测
        for u in _from_registry(strict=False):
            add("注册表里记录的代理", u)
        for p in detect_proxy_ports(limit=4):
            add(f"本机端口 {p}", f"http://127.0.0.1:{p}")
    add("直连", None)
    return outlets


# 探测用的极小查询：上海人民广场一小块里的建筑数量
PROBE_QUERY = ('[out:json][timeout:8];'
               'way["building"](31.2290,121.4710,31.2310,121.4740);'
               'out count;')


def _local_port_closed(proxy_url):  # noqa: D401
    """proxy 是 127.0.0.1:xxxx 且端口没在监听 → True。"""
    try:
        u = urllib.parse.urlsplit(normalize(proxy_url) or "")
        host, port = u.hostname or "", u.port
    except ValueError:
        return False
    if host not in ("127.0.0.1", "localhost", "::1") or not port:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.6)
            return s.connect_ex(("127.0.0.1", port)) != 0
    except Exception:  # noqa: BLE001
        return False


def probe_outlet(proxy, urls, timeout=5):
    """判断这个出口能不能正经查 Overpass。

    这里**不能**用 /api/status 探活 —— 那是另一条路径，很多线路（GFW、VPN、
    服务器自己的防护）恰恰只拦它，于是「明明能查数据」却被误判成「出口不通」。
    直接用一条真实的小查询，返回 JSON 才算通。

    多个镜像并发试，谁先回 JSON 就算谁。
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)

    payload = urllib.parse.urlencode({"data": PROBE_QUERY}).encode("utf-8")
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 MapToCAD/1.2"),
        "Accept": "application/json, */*",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # 本机代理软件已经退出了的话，端口直接是关的 —— 立刻返回，
    # 别再去试 7 个镜像白等（这种情况最常见：关掉加速器后忘了）
    if proxy and _local_port_closed(proxy):
        return None

    def one(url):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            with opener.open(req, timeout=timeout) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
            els = obj.get("elements") or []
            if not els:
                return None
            # out count 的返回：total=0 说明这个镜像是区域性的（如 osm.ch 只有瑞士），
            # 对你框的地方没数据，当它不可用
            total = (els[0].get("tags") or {}).get("total")
            if total is not None and str(total) == "0":
                return None
            return url
        except urllib.error.HTTPError:
            # 只有真拿到 JSON 才算通。5xx/429 看着像"线路通、服务器忙"，
            # 但实测经常是线路本身有问题（返回的是中间设备的错误页），
            # 拿它当"通"会让工具在一个死出口上白等好几分钟。
            return None
        except Exception:  # noqa: BLE001
            return None

    urls = list(urls)
    if not urls:
        return None
    with ThreadPoolExecutor(max_workers=len(urls)) as ex:
        for r in ex.map(one, urls):
            if r:
                return r
    return None


def describe():
    """给界面用：当前检测到的网络出口情况。"""
    env = _from_env()
    reg = _from_registry()
    hint = _from_registry(strict=False)
    ports = detect_proxy_ports()
    return {
        "env": env,
        "registry": reg,
        "registry_hint": hint,
        "local_ports": ports,
        "has_proxy": bool(env or reg or ports),
        "outlets": [{"label": lb, "proxy": u} for lb, u in build_outlets(scan=False)],
    }
