"""端到端验证：模拟 OSM 官方接口 + 模拟 HTTP 代理（不需要联网）。

直接跑：runtime/python.exe test_proxy.py

要点：
  * 模拟服务器只在请求头带 X-Via-Proxy 时才应答（403 否则）
    → 能出结果就证明流量真的走了代理，不是直连碰巧通了
  * 已经删掉 Overpass 通道，抓取直接走 OSM 官方接口 /api/0.6/map，
    这里验证「官方接口 + 代理」这条链路
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SRV_PORT = 9801
# 故意用一个在 netproxy 端口扫描列表里的端口，好顺便验证"自动识别"
# 万一被你的代理软件占着，就退到别的空闲端口（那几项断言会跳过）
PROXY_PORT = None
AUTO_DETECT_OK = False


def _bindable(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_proxy_port():
    """优先用扫描列表里的端口；都被占用就随便给一个空闲的。"""
    global AUTO_DETECT_OK
    for p in (7897, 7893, 7892, 7891, 7890):
        if _bindable(p):
            AUTO_DETECT_OK = True
            return p
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


BBOX = [121.4700, 31.2300, 121.4720, 31.2320]

stats = {"status": 0, "map": 0, "via_proxy": 0, "blocked": 0, "http504": 0}


# OSM 官方接口返回的 XML：一栋建筑（闭合 way）+ 一条路（带中文名）
def _osm_xml():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="mock">
  <node id="1" lat="31.2300" lon="121.4700"/>
  <node id="2" lat="31.2300" lon="121.4708"/>
  <node id="3" lat="31.2308" lon="121.4708"/>
  <node id="4" lat="31.2308" lon="121.4700"/>
  <node id="5" lat="31.2300" lon="121.4700"/>
  <node id="6" lat="31.2320" lon="121.4720"/>
  <way id="1001">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
  </way>
  <way id="2001">
    <nd ref="5"/><nd ref="6"/>
    <tag k="highway" v="primary"/>
    <tag k="name" v="测试大道"/>
  </way>
</osm>"""
    return xml.encode("utf-8")


class MockOverpass(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _via_proxy(self):
        if self.headers.get("X-Via-Proxy") == "1":
            stats["via_proxy"] += 1
            return True
        stats["blocked"] += 1
        self.send_response(403)
        body = b"direct connection not allowed in test"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        if not self._via_proxy():
            return
        if self.path.startswith("/api/status"):
            stats["status"] += 1
            body = b"Connected as: 1\nCurrent time: test\nRate limit: 2\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # OSM 官方接口：/api/0.6/map?bbox=w,s,e,n
        if "/api/0.6/map" in self.path:
            stats["map"] += 1
            body = _osm_xml()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        body = b"not found"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._via_proxy():
            return
        self.send_response(405)
        body = b"method not allowed"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def proxy_worker(conn):
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        method, target, _ver = lines[0].split(b" ")
        clen, hdrs = 0, []
        for ln in lines[1:]:
            if b":" not in ln:
                continue
            k, v = ln.split(b":", 1)
            k, v = k.strip(), v.strip()
            if k.lower() == b"content-length":
                clen = int(v)
            hdrs.append((k, v))
        body = rest
        while len(body) < clen:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk

        u = urllib.parse.urlsplit(target.decode())

        # CONNECT：真的代理必须支持，否则 _looks_like_proxy 认不出来
        if method == b"CONNECT":
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            host, _, port_s = target.decode().partition(":")
            try:
                up = socket.create_connection((host, int(port_s or 443)), timeout=6)
            except Exception:  # noqa: BLE001
                return

            def pump(src, dst):
                try:
                    while True:
                        b = src.recv(65536)
                        if not b:
                            break
                        dst.sendall(b)
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=pump, args=(conn, up), daemon=True).start()
            pump(up, conn)
            up.close()
            return

        host, port = u.hostname, u.port or 80
        path = u.path + ("?" + u.query if u.query else "")
        out = f"{method.decode()} {path} HTTP/1.1\r\n".encode()
        for k, v in hdrs:
            if k.lower() in (b"connection", b"proxy-connection", b"host"):
                continue
            out += k + b": " + v + b"\r\n"
        out += f"Host: {host}:{port}\r\n".encode()
        out += b"X-Via-Proxy: 1\r\nConnection: close\r\n\r\n"

        up = socket.create_connection((host, port), timeout=15)
        up.sendall(out + body)
        while True:
            chunk = up.recv(65536)
            if not chunk:
                break
            conn.sendall(chunk)
        up.close()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def start_proxy():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", PROXY_PORT))
    s.listen(32)

    def loop():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=proxy_worker, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return s


def main():
    global SRV_PORT, PROXY_PORT
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + name + (f"   {extra}" if extra else ""))
        if not cond:
            ok = False

    PROXY_PORT = pick_proxy_port()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockOverpass)
    SRV_PORT = srv.server_address[1]
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    psock = start_proxy()
    time.sleep(0.5)

    # 把 OSM 官方接口重定向到本地 mock 服务器（和 Overpass 的 MAP2CAD_MIRRORS 同理）
    os.environ["MAP2CAD_OSMAPI"] = f"http://127.0.0.1:{SRV_PORT}/api/0.6/map"

    import importlib
    from map2cad import netproxy, overpass, osmapi
    importlib.reload(overpass)
    importlib.reload(osmapi)

    print(f"（模拟代理端口 {PROXY_PORT}，模拟 OSM 官方接口端口 {SRV_PORT}）")

    # ---- 1. 代理地址解析 ----
    print("[1] 自动识别本机代理端口")
    from map2cad.netproxy import _scan_local_ports, build_outlets
    netproxy._scan_cache["t"] = 0
    ports = _scan_local_ports(use_cache=False)
    if AUTO_DETECT_OK:
        chk(f"扫到了模拟代理端口 {PROXY_PORT}", PROXY_PORT in ports, str(ports))
        outs = build_outlets(scan=True)
        chk("出口列表里包含该代理",
            any((u or "").endswith(f":{PROXY_PORT}") for _, u in outs if u), str(outs))
    else:
        print("  --   跳过：扫描列表里的端口都被占用，改用随机端口测显式代理")

    # ---- 1b. 只把真的 HTTP 代理当成代理，别把无关服务算进来 ----
    print("[1b] 代理识别要准（普通服务不能被当成代理）")
    chk("模拟代理被认出来", netproxy._looks_like_proxy(PROXY_PORT), str(PROXY_PORT))
    chk("普通 HTTP 服务不被当成代理", not netproxy._looks_like_proxy(SRV_PORT),
        f"{SRV_PORT} 是模拟 OSM 官方接口")
    if AUTO_DETECT_OK:
        chk("describe() 会把它列出来", PROXY_PORT in netproxy.detect_proxy_ports(),
            str(netproxy.detect_proxy_ports()))
    else:
        print("  --   跳过：随机端口不在扫描列表里，属预期")

    # ---- 2. 手动填代理 ----
    print("[2] 手动指定代理时能抓到数据（流量必须过代理）")
    before = dict(stats)
    fc, st = overpass.fetch_geojson(BBOX, ["building", "highway"], clip=True,
                                    proxy=f"http://127.0.0.1:{PROXY_PORT}",
                                    log=lambda m: None)
    chk("拿到要素", len(fc["features"]) > 0, f"{len(fc['features'])} 个")
    chk("确实走了代理", stats["via_proxy"] > before["via_proxy"],
        f"via_proxy={stats['via_proxy']} blocked={stats['blocked']}")
    chk("中文路名保留", any(f["properties"].get("name") == "测试大道"
                            for f in fc["features"]))
    chk("有建筑面图层", any(f["properties"]["layer"] == "建筑" for f in fc["features"]),
        str(sorted({f["properties"]["layer"] for f in fc["features"]})))
    chk("数据源标记为 osm-api", (fc.get("meta") or {}).get("source") == "osm-api",
        str(fc.get("meta", {}).get("source")))

    # ---- 3. 不填代理：应当自动扫到并走代理 ----
    print("[3] 代理留空时自动生效（模拟速博士全局模式）")
    overpass._good_outlet["key"] = None
    overpass._good_outlet["url"] = None
    before = dict(stats)
    logs = []
    # 真实机器上可能还开着别的代理，这个用例只允许走模拟代理，才是确定性测试
    os.environ["MAP2CAD_ONLY_OUTLET"] = f"http://127.0.0.1:{PROXY_PORT}"
    try:
        fc2, _ = overpass.fetch_geojson(BBOX, ["building"], clip=True,
                                        proxy=None, log=logs.append)
    finally:
        os.environ.pop("MAP2CAD_ONLY_OUTLET", None)
    chk("不填代理也能抓到", len(fc2["features"]) > 0, f"{len(fc2['features'])} 个")
    chk("是自动走代理抓的", stats["via_proxy"] > before["via_proxy"], "")
    chk("日志里说明了用的出口",
        any("出口" in x for x in logs),
        " | ".join(x for x in logs if "出口" in x)[:110])

    # ---- 4. 网络诊断接口 ----
    print("[4] 网络诊断返回结构")
    d = overpass.test_mirrors(proxy=f"http://127.0.0.1:{PROXY_PORT}")
    chk("有 outlets/active/ok 字段",
        {"outlets", "active", "ok"} <= set(d), str(list(d)))
    chk("active 指向可用出口", bool(d["active"]), str(d["active"]))
    chk("官方接口可用", d["ok"], str([r["msg"] for r in d["results"]]))

    # ---- 5. 全都不通时的报错是否友好 ----
    print("[5] 出口全不通时的提示")
    srv.shutdown()
    time.sleep(0.3)
    overpass._good_outlet["key"] = None
    overpass._good_outlet["url"] = None
    # 指向一个绝对没人监听的端口，保证"全都不通"这个前提成立；
    # 同时把瓦片兜底通道也指向没人监听的端口，否则它可能真连上境外 CDN。
    os.environ["MAP2CAD_ONLY_OUTLET"] = "http://127.0.0.1:9"
    os.environ["MAP2CAD_TILES"] = "http://127.0.0.1:9/tiles/osm/{z}/{x}/{y}"
    from map2cad import tiles
    importlib.reload(tiles)
    try:
        overpass.fetch_geojson(BBOX, ["building"], proxy=None,
                               timeout=3, log=lambda m: None)
        chk("应当报错却没有", False)
    except overpass.FetchError as e:
        chk("抛出 FetchError", True)
        chk("错误类型是 network", e.kind == "network", e.kind)
        chk("提示里有可操作建议",
            "代理" in str(e) or "网络出口" in str(e), str(e)[:70])

    os.environ.pop("MAP2CAD_ONLY_OUTLET", None)
    os.environ.pop("MAP2CAD_OSMAPI", None)
    os.environ.pop("MAP2CAD_TILES", None)
    psock.close()
    print()
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
