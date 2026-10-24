"""本地服务：网页界面 + OSM 抓取 + GeoJSON/ DXF 转换。

只依赖 Python 标准库 + ezdxf。
"""
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)

VERSION = "1.12"

sys.path.insert(0, str(ROOT))

from map2cad.dxf_export import export_dxf           # noqa: E402
from map2cad.overpass import LAYER_DEFS, fetch_geojson  # noqa: E402

TASKS = {}
TASKS_LOCK = threading.Lock()


def _new_task():
    tid = f"t{int(time.time()*1000)}"
    with TASKS_LOCK:
        TASKS[tid] = {"state": "running", "log": [], "result": None, "error": None}
    return tid


def _log(tid, msg):
    with TASKS_LOCK:
        t = TASKS.get(tid)
        if t is not None:
            t["log"].append(msg)


def _finish(tid, result=None, error=None):
    with TASKS_LOCK:
        t = TASKS.get(tid)
        if t is None:
            return
        t["state"] = "error" if error else "done"
        t["result"] = result
        t["error"] = error


def _safe_name(s):
    keep = [c for c in str(s) if c.isalnum() or c in "-_"]
    return "".join(keep)[:40] or "map"


class Handler(BaseHTTPRequestHandler):
    server_version = "MapToCAD/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    # ---------- 工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(),
                              "text/html; charset=utf-8")
        if path == "/api/layers":
            return self._send(200, {
                "layers": [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "filters"}}
                           for k, v in LAYER_DEFS.items()]
            })
        if path == "/api/ping":
            return self._send(200, f"pong/{VERSION}", "text/plain; charset=utf-8")
        if path == "/api/demo":
            from map2cad.demo import build_demo_geojson
            fc = build_demo_geojson()
            stamp = time.strftime("%Y%m%d_%H%M%S")
            gpath = OUT / f"演示数据_{stamp}.geojson"
            gpath.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
            stats = {}
            for f in fc["features"]:
                lay = f["properties"]["layer"]
                stats[lay] = stats.get(lay, 0) + 1
            return self._send(200, {"geojson_file": gpath.name, "count": len(fc["features"]),
                                    "stats": dict(sorted(stats.items(), key=lambda kv: -kv[1])),
                                    "preview": fc, "bbox": fc["bbox"], "demo": True})
        if path == "/api/testnet":
            proxy = (qs.get("proxy") or [""])[0] or None
            from map2cad.overpass import test_mirrors
            return self._send(200, test_mirrors(proxy=proxy))
        if path == "/api/geocode":
            q_ = (qs.get("q") or [""])[0]
            proxy = (qs.get("proxy") or [""])[0].strip() or None
            from map2cad.geocode import search as geo_search
            try:
                return self._send(200, {"results": geo_search(q_, proxy=proxy)})
            except Exception as e:  # noqa: BLE001
                return self._send(200, {"results": [], "error": str(e)[:160]})
        if path == "/api/proxyinfo":
            from map2cad.netproxy import describe
            return self._send(200, describe())
        if path == "/api/status":
            tid = (qs.get("id") or [""])[0]
            with TASKS_LOCK:
                t = TASKS.get(tid)
                return self._send(200, t or {"state": "missing"})
        if path == "/api/download":
            fname = (qs.get("f") or [""])[0]
            target = (OUT / os.path.basename(fname)).resolve()
            if not str(target).startswith(str(OUT.resolve())) or not target.exists():
                return self._send(404, {"error": "文件不存在"})
            ctype = {".dxf": "application/dxf",
                     ".jpg": "image/jpeg",
                     ".png": "image/png"}.get(
                         target.suffix.lower(), "application/geo+json")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(target.stat().st_size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{target.name}"')
            self.end_headers()
            try:
                with open(target, "rb") as fh:
                    while True:
                        chunk = fh.read(262144)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return

        # 静态文件
        rel = path.lstrip("/")
        target = (WEB / rel).resolve()
        if str(target).startswith(str(WEB.resolve())) and target.is_file():
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix in (".js", ".css", ".html"):
                ctype += "; charset=utf-8"
            return self._send(200, target.read_bytes(), ctype)
        return self._send(404, {"error": "not found"})

    # ---------- POST ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self._read_json()
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "请求格式错误"})

        if path == "/api/fetch":
            return self._handle_fetch(data)
        if path == "/api/convert":
            return self._handle_convert(data)
        if path == "/api/import":
            return self._handle_import(data)
        if path == "/api/shutdown":
            self._send(200, {"ok": True})
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
        return self._send(404, {"error": "not found"})

    # ---------- 业务 ----------
    def _handle_fetch(self, data):
        bbox = data.get("bbox")
        layers = data.get("layers") or ["building", "highway", "water"]
        proxy = (data.get("proxy") or "").strip() or None
        clip = bool(data.get("clip", True))
        name = _safe_name(data.get("name") or "map")

        if not bbox or len(bbox) != 4:
            return self._send(400, {"error": "bbox 参数不对"})

        tid = _new_task()

        def work():
            try:
                geojson, stats = fetch_geojson(
                    bbox, layers, proxy=proxy, clip=clip,
                    log=lambda m: _log(tid, m))
                stamp = time.strftime("%Y%m%d_%H%M%S")
                base = f"{name}_{stamp}"
                gpath = OUT / f"{base}.geojson"
                gpath.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
                count = len(geojson["features"])
                _log(tid, f"GeoJSON 已保存：{gpath.name}")
                preview = geojson if count <= 20000 else None
                if preview is None:
                    _log(tid, "要素较多（>2 万），网页跳过叠加预览，但文件是完整的")
                _finish(tid, {
                    "geojson_file": gpath.name,
                    "count": count,
                    "stats": stats,
                    "preview": preview,
                    "bbox": bbox,
                    "meta": geojson.get("meta") or {},
                })
            except Exception as e:  # noqa: BLE001
                _log(tid, f"出错：{e}")
                _finish(tid, error=f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()
        return self._send(200, {"task": tid})

    def _handle_import(self, data):
        """用户从 overpass-turbo 或别处拿到的 GeoJSON / OSM JSON，直接导入。"""
        raw = data.get("content")
        bbox = data.get("bbox")
        name = _safe_name(data.get("name") or "imported")
        if not raw:
            return self._send(400, {"error": "没有内容"})
        try:
            obj = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            return self._send(400, {"error": f"不是合法 JSON：{e}"})

        try:
            if obj.get("type") == "FeatureCollection":
                geojson = obj
                stats = {}
                for f in geojson["features"]:
                    lay = (f.get("properties") or {}).get("layer", "其他")
                    stats[lay] = stats.get(lay, 0) + 1
            elif "elements" in obj:
                if not bbox:
                    return self._send(400, {"error": "OSM 原始数据需要提供 bbox"})
                from map2cad.overpass import raw_to_geojson
                geojson, stats = raw_to_geojson(obj, bbox, clip=bool(data.get("clip", True)))
            else:
                return self._send(400, {"error": "认不出的数据格式（要 GeoJSON 或 Overpass JSON）"})

            stamp = time.strftime("%Y%m%d_%H%M%S")
            base = f"{name}_{stamp}"
            gpath = OUT / f"{base}.geojson"
            gpath.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
            count = len(geojson["features"])
            return self._send(200, {
                "geojson_file": gpath.name, "count": count, "stats": stats,
                "preview": geojson if count <= 20000 else None,
                "bbox": bbox or geojson.get("bbox"),
            })
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _handle_convert(self, data):
        gfile = data.get("geojson_file")
        if not gfile:
            return self._send(400, {"error": "缺少 geojson_file"})
        gpath = (OUT / os.path.basename(gfile)).resolve()
        if not gpath.exists():
            return self._send(404, {"error": "源文件不存在，请重新抓取"})

        # 先同步读完源文件（这一步很快，卡住不是它的问题）；
        # 之后耗时的抓卫星图 / 算路网轮廓放到后台线程，前端轮询 /api/status 看进度。
        try:
            geojson = json.loads(gpath.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return self._send(400, {"error": f"GeoJSON 读不出来：{e}"})

        out_path = gpath.with_suffix(".dxf")
        opts = {
            "text_height": float(data.get("text_height") or 2.5),
            "road_mode": data.get("road_mode") or "band",
            "label_mode": data.get("label_mode") or "all",
            "info_text": bool(data.get("info_text", True)),
            "road_outline": bool(data.get("road_outline", True)),
            "road_fill": bool(data.get("road_fill", False)),
            "satellite": bool(data.get("satellite", True)),
        }
        n_feats = len(geojson.get("features", []))

        tid = _new_task()

        def work():
            try:
                _log(tid, f"开始转换：{n_feats} 个要素 → DXF")
                result = export_dxf(geojson, str(out_path),
                                    log=lambda m: _log(tid, m), **opts)
                result["dxf_file"] = out_path.name
                _finish(tid, result)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                _log(tid, f"出错：{type(e).__name__}: {e}")
                _finish(tid, error=f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()
        return self._send(200, {"task": tid})


def _port_in_use(port, timeout=0.4):
    """用"能不能连上"来判断端口是否被占。

    不能用 bind 试：Windows 上设置了 SO_REUSEADDR 的 bind 会"抢占"已被监听的端口
    并且不报错，结果就是两个进程都以为自己占了 8765。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pick_port(start=8765, tries=40):
    """返回 (可用端口, 被占用的端口列表)。"""
    busy = []
    for p in range(start, start + tries):
        if _port_in_use(p):
            busy.append(p)
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                busy.append(p)
                continue
        return p, busy
    raise RuntimeError("找不到可用端口")


def _is_our_instance(port):
    """这个端口上跑的是不是本工具的另一个实例。"""
    return _instance_version(port) is not None


def _instance_version(port):
    """返回该端口上本工具的版本号；不是本工具返回 None。"""
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(f"http://127.0.0.1:{port}/api/ping", timeout=0.8) as r:
            txt = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    if not txt.startswith("pong"):
        return None
    parts = txt.split("/", 1)
    return parts[1] if len(parts) > 1 else "0"


def _shutdown_instance(port):
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        op.open(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", timeout=2).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    port, busy = _pick_port()

    # 端口被占：是本工具的另一个实例？顺手处理掉，避免用户看到旧版本界面
    for b in busy:
        v = _instance_version(b)
        if v is None:
            continue
        if v != VERSION:
            print(f"发现旧版本还在运行（端口 {b}），正在关掉并换成新版…")
            _shutdown_instance(b)
            # 等旧进程真正退出，尽量还回到原来那个端口（用户书签不会变）
            for _ in range(10):
                time.sleep(0.4)
                if not _port_in_use(b):
                    break
            port, busy = _pick_port()
            break
        print(f"工具已经在运行了（端口 {b}），正在帮你打开浏览器…")
        webbrowser.open(f"http://127.0.0.1:{b}/")
        time.sleep(1.5)
        return

    url = f"http://127.0.0.1:{port}/"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print("=" * 60)
    print("  免费矢量地图 -> CAD 工具已启动")
    print(f"  浏览器地址：{url}")
    print(f"  输出目录：{OUT}")
    print("  关闭这个黑窗口 = 关闭工具")
    print("=" * 60)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
