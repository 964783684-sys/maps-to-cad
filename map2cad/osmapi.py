"""OSM 官方地图接口：抓取矢量数据的主通道。

为什么用它做主通道：
  Overpass 的镜像（overpass-api.de / kumi / private.coffee …）最近几年经常
  直接拒绝中国方向、或者拒绝数据中心/VPN 出口 IP，返回 406 / 502 / 504，
  或者干脆把连接掐掉。这时候无论怎么换镜像、换代理都没用 —— 是对方不收。
  所以干脆跳过 Overpass，直接走官方接口，省掉每次白等几分钟超时的时间。

      GET https://api.openstreetmap.org/api/0.6/map?bbox=左,下,右,上
  免费、不需要 key，走的是和 Overpass 同一份 OpenStreetMap 数据。

它的代价：
  * 只能给「框里的全部要素」，没法按标签过滤 —— 所以抓回来要自己按图层筛
  * 单次有上限：面积 ≤ 0.25 平方度、节点 ≤ 5 万 —— 超了这里会自动切块重抓
  * 数据量比按标签查大不少（1 公里框约 2~3 MB）
"""
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

API = "https://api.openstreetmap.org/api/0.6/map"

# 调试/离线测试用：MAP2CAD_OSMAPI=http://127.0.0.1:9801/api/0.6/map
# 让单元测试能用一个本地 mock 服务器替代真实的官方接口，不联网也能跑。
_env_api = os.environ.get("MAP2CAD_OSMAPI")
if _env_api:
    API = _env_api.strip()

# 官方要求 UA 能标识用途，别用浏览器 UA 冒充；也方便他们限流时区分
UA = "MapToCAD/1.2 (free OSM vector to DXF converter; low-volume one-off use)"

MAX_AREA = 0.24        # 平方度（官方上限 0.25，留点余量）
MAX_DEPTH = 3          # 最多切到 4x4（16 块）
REQUEST_GAP = 0.25     # 块与块之间的间隔，别把官方服务器打急


class SourceError(RuntimeError):
    """带原因分类，方便上层决定「换出口 / 切块 / 放弃」。"""

    def __init__(self, msg, kind="unknown"):
        super().__init__(msg)
        self.kind = kind    # network / busy / too_big / denied


def _opener(proxy=None):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _url(bbox):
    w, s, e, n = bbox
    return "%s?bbox=%.7f,%.7f,%.7f,%.7f" % (API, w, s, e, n)


def _get(bbox, opener, timeout):
    req = urllib.request.Request(_url(bbox), headers={
        "User-Agent": UA,
        "Accept": "application/xml, text/xml, */*",
    })
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def fetch_xml(bbox, opener, timeout=75, tries=4, log=None):
    """取一块区域的原始 OSM XML，遇到断连/超时会重试。

    这类代理线路最常见的问题就是「第一次通、第二次被掐」，所以必须重试。
    """
    w, s, e, n = bbox
    if (e - w) * (n - s) > MAX_AREA:
        raise SourceError("区域超过官方单次上限", "too_big")

    last = None
    for i in range(tries):
        t0 = time.time()
        try:
            raw = _get(bbox, opener, timeout)
            if not raw.lstrip().startswith(b"<?xml") and b"<osm" not in raw[:400]:
                raise SourceError("返回内容不是 OSM 数据（可能被拦截）", "denied")
            if log and i:
                log(f"    第 {i + 1} 次才通，{len(raw) / 1024:.0f} KB，"
                    f"{time.time() - t0:.1f}s")
            return raw
        except urllib.error.HTTPError as ex:
            body = ""
            try:
                body = ex.read(600).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            low = body.lower()
            if ex.code in (400, 413) and ("too many" in low or "node" in low):
                raise SourceError("这块要素太多（超过 5 万节点）", "too_big")
            if ex.code in (429, 509):
                last = SourceError(f"HTTP {ex.code}（官方限流）", "busy")
            elif ex.code in (400, 401, 403, 404, 405):
                raise SourceError(f"HTTP {ex.code}", "denied")
            else:
                last = SourceError(f"HTTP {ex.code}", "busy")
        except SourceError:
            raise
        except Exception as ex:  # noqa: BLE001  SSL EOF / 连接重置 / 超时
            last = SourceError(f"{type(ex).__name__}" + (f": {str(ex)[:60]}" if str(ex) else ""),
                               "network")
        if i < tries - 1:
            time.sleep(min(6.0, 1.0 + i * 1.5))
    raise last or SourceError("取数据失败", "network")


def _runs(refs, nodes):
    """把一条 way 的节点引用切成「连续可用的段」（节点可能因裁剪而缺失）。"""
    runs, cur = [], []
    for r in refs:
        p = nodes.get(r)
        if p is None:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
        else:
            cur.append(p)
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def parse_xml(raw):
    """OSM XML -> 和 Overpass `out geom` 一样的 elements 结构。

    这样后面可以直接复用 osm_to_geojson()，不用写第二套转换逻辑。
    """
    root = ET.fromstring(raw)

    nodes = {}
    node_elems, way_elems, rel_elems = [], [], []
    for el in root:
        if el.tag == "node":
            nid = el.get("id")
            try:
                lon, lat = float(el.get("lon")), float(el.get("lat"))
            except (TypeError, ValueError):
                continue
            nodes[nid] = (lon, lat)
            tags = {t.get("k"): t.get("v") for t in el if t.tag == "tag"}
            if tags:      # 无标签的节点只是几何用，不用进 elements
                node_elems.append({"type": "node", "id": nid, "lat": lat, "lon": lon,
                                   "tags": tags})
        elif el.tag == "way":
            way_elems.append(el)
        elif el.tag == "relation":
            rel_elems.append(el)

    elements = list(node_elems)
    way_geom = {}          # way id -> [(lon,lat)…]，relation 成员要用

    for el in way_elems:
        wid = el.get("id")
        refs = [nd.get("ref") for nd in el if nd.tag == "nd"]
        runs = _runs(refs, nodes)
        if not runs:
            continue
        # 缺口两侧各成一段；多于一段时给后面的加后缀，避免被当成同一个要素去重
        way_geom[wid] = runs[0]
        tags = {t.get("k"): t.get("v") for t in el if t.tag == "tag"}
        if not tags:
            continue
        for i, run in enumerate(runs):
            elements.append({
                "type": "way",
                "id": wid if i == 0 else f"{wid}#{i}",
                "tags": tags,
                "geometry": [{"lon": x, "lat": y} for x, y in run],
            })

    for el in rel_elems:
        tags = {t.get("k"): t.get("v") for t in el if t.tag == "tag"}
        if not tags:
            continue
        members = []
        for m in el:
            if m.tag != "member" or m.get("type") != "way":
                continue
            geom = way_geom.get(m.get("ref"))
            if not geom:
                continue
            members.append({
                "type": "way",
                "ref": m.get("ref"),
                "role": m.get("role") or "outer",
                "geometry": [{"lon": x, "lat": y} for x, y in geom],
            })
        if members:
            elements.append({"type": "relation", "id": el.get("id"),
                             "tags": tags, "members": members})

    return elements


def _split(bbox, n=2):
    """把框切成 n x n 块。"""
    w, s, e, n_ = bbox
    dx, dy = (e - w) / n, (n_ - s) / n
    out = []
    for i in range(n):
        for j in range(n):
            out.append([w + i * dx, s + j * dy, w + (i + 1) * dx, s + (j + 1) * dy])
    return out


def _area(bbox):
    w, s, e, n = bbox
    return max(0.0, (e - w) * (n - s))


def probe(proxy=None, timeout=12, tries=4):
    """用一个 20 米的小框确认这个出口能不能用官方接口。

    必须重试：代理线路上「第一次就被掐断」很常见，一次失败说明不了一切。
    """
    # 上海人民广场附近的一个极小块，正常会返回 1~3 KB
    tiny = [121.47360, 31.23040, 121.47380, 31.23060]
    op = _opener(proxy)
    for i in range(tries):
        try:
            raw = _get(tiny, op, timeout)
            if b"<osm" in raw[:400]:
                return True
        except Exception:  # noqa: BLE001
            pass
        if i < tries - 1:
            time.sleep(0.8 + i)
    return False


def fetch_elements(bbox, outlets, log=None, timeout=75, budget=300):
    """按出口顺序试，返回 (elements, 用到的出口地址或 None, 分块数)。

    outlets: [(说明, 代理地址或 None), …]，由调用方给（复用 Overpass 那套顺序）
    """
    deadline = time.time() + budget
    last = None

    for label, proxy in outlets:
        if time.time() > deadline:
            break
        if log:
            log(f"  [{label}] 用官方地图接口试一下（{proxy or '直连'}）")
        if not probe(proxy, timeout=7, tries=2):
            if log:
                log("    × 这个小测试就取不到数据，换下一个出口")
            continue

        acc = []
        tiles = {"ok": 0, "fail": 0}

        def collect(b, depth):
            if time.time() > deadline:
                raise SourceError("时间到了", "network")
            try:
                raw = fetch_xml(b, _opener(proxy), timeout=timeout,
                                tries=4 if depth == 0 else 3, log=log)
                got = parse_xml(raw)
                acc.extend(got)
                tiles["ok"] += 1
                if log:
                    log(f"    ✓ 第 {tiles['ok']} 块：{len(got)} 个原始要素")
                return
            except SourceError as ex:
                if ex.kind != "too_big" or depth >= MAX_DEPTH:
                    raise
            subs = _split(b, 2)
            if log:
                log(f"    这块太大，切成 {len(subs)} 小块再抓（第 {depth + 2} 层）")
            errs = []
            for sb in subs:
                try:
                    collect(sb, depth + 1)
                except SourceError as ex:
                    tiles["fail"] += 1
                    errs.append(ex)
                    # 失败的小块别静默丢掉：立刻重试一次，再不行就把缺的块记下来，
                    # 让上层知道「这块区域没抓到」，而不是假装成功了。
                    if log:
                        log(f"    ⚠ 有一小块没抓到（{ex.kind}）：{str(ex)[:60]}，重试一次")
                    time.sleep(REQUEST_GAP * 2)
                    try:
                        collect(sb, depth + 1)
                    except SourceError as ex2:
                        if log:
                            log(f"    ✗ 重试还是不行，这块区域会缺数据")
                time.sleep(REQUEST_GAP)

        try:
            collect(list(bbox), 0)
            if not acc:
                raise SourceError("取回来的数据是空的", "busy")
            if tiles["fail"]:
                if log:
                    log(f"    ⚠ 有 {tiles['fail']} 个小块最终没抓到，"
                        "对应区域会缺数据（可缩小范围或稍后重试）")
            return acc, proxy, tiles["ok"], tiles["fail"]
        except SourceError as ex:
            last = ex
            if log:
                log(f"    × 这个出口不行（{ex.kind}）：{str(ex)[:80]}")
            if ex.kind == "denied" and not proxy:
                continue
            continue

    raise last or SourceError("官方接口的所有出口都不通", "network")
