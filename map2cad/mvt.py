"""最小 MVT(Mapbox Vector Tile) 解码器：零依赖，纯标准库。

只实现读取需要的部分：
  Tile.layers -> Layer{name, extent, keys, values, features[]}
  Feature{id, tags, type, geometry(瓦片内整数坐标)}
几何命令：1=MoveTo 2=LineTo 7=ClosePath，参数按 zigzag 编码。
"""
from __future__ import annotations


def _varint(buf, i):
    """读一个 varint，返回 (值, 新位置)。"""
    result = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint 太长")


def _zigzag(n):
    return (n >> 1) ^ (-(n & 1))


def _fields(buf):
    """把一段 protobuf 拆成 (字段号, 类型, 值) 序列。类型 0=varint 2=长度前缀。"""
    i = 0
    n = len(buf)
    while i < n:
        key, i = _varint(buf, i)
        fid = key >> 3
        wt = key & 7
        if wt == 0:
            val, i = _varint(buf, i)
            yield fid, 0, val
        elif wt == 2:
            ln, i = _varint(buf, i)
            yield fid, 2, buf[i:i + ln]
            i += ln
        elif wt == 5:          # fixed32
            yield fid, 5, buf[i:i + 4]
            i += 4
        elif wt == 1:          # fixed64
            yield fid, 1, buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"不支持的 wire type {wt}")


def _packed_varints(buf):
    out = []
    i = 0
    while i < len(buf):
        v, i = _varint(buf, i)
        out.append(v)
    return out


def decode(buf: bytes) -> dict:
    """解析一个 .pbf 瓦片，返回 {layer_name: {"extent":int, "features":[...]}}。"""
    layers = {}
    for fid, wt, val in _fields(buf):
        if fid != 3 or wt != 2:
            continue
        name = ""
        extent = 4096
        keys, values, feats = [], [], []
        for f2, w2, v2 in _fields(val):
            if f2 == 1 and w2 == 2:
                name = v2.decode("utf-8", "replace")
            elif f2 == 2 and w2 == 2:                    # features
                feats.append(v2)
            elif f2 == 3 and w2 == 2:                    # keys
                keys.append(v2.decode("utf-8", "replace"))
            elif f2 == 4 and w2 == 2:                    # values
                values.append(_decode_value(v2))
            elif f2 == 5 and w2 == 0:                    # extent
                extent = v2
        out = []
        for fb in feats:
            f = _decode_feature(fb, keys, values)
            if f:
                out.append(f)
        layers[name] = {"extent": extent, "features": out}
    return layers


def _decode_value(buf):
    """按 MVT 规范解 Value（字段号别搞错，否则 bridge/rail 这类布尔量会全丢）。

    规范里的字段：1=string 2=float 3=double 4=int64 5=uint64 6=sint64 7=bool
    """
    for fid, wt, val in _fields(buf):
        if fid == 1 and wt == 2:
            return val.decode("utf-8", "replace")
        if fid == 2 and wt == 5:
            import struct
            return struct.unpack("<f", val)[0]
        if fid == 3 and wt == 1:                      # double，定长 8 字节
            import struct
            return struct.unpack("<d", val)[0]
        if fid == 4 and wt == 0:                      # int64（补码）
            return val - (1 << 64) if val >> 63 else val
        if fid == 5 and wt == 0:                      # uint64
            return val
        if fid == 6 and wt == 0:                      # sint64（zigzag）
            return (val >> 1) ^ -(val & 1)
        if fid == 7 and wt == 0:                      # bool
            return bool(val)
    return None


def _decode_feature(buf, keys, values):
    fid = 0
    tags = []
    ftype = 0
    geom = []
    for f, wt, val in _fields(buf):
        if f == 1 and wt == 0:
            fid = val
        elif f == 2 and wt == 2:
            tags = _packed_varints(val)
        elif f == 3 and wt == 0:
            ftype = val
        elif f == 4 and wt == 2:
            geom = _packed_varints(val)
    props = {}
    for i in range(0, len(tags) - 1, 2):
        ki, vi = tags[i], tags[i + 1]
        if ki < len(keys) and vi < len(values):
            props[keys[ki]] = values[vi]
    return {"id": fid, "type": ftype, "tags": props,
            "geometry": _rings(geom, ftype)}


def _rings(cmds, ftype=0):
    """把命令流解成 [(x,y), ...] 的若干段。

    ftype：1=点 2=线 3=面。点要素只有 1 个坐标，别被当成无效几何丢掉。
    """
    min_len = 1 if ftype == 1 else 2
    out = []
    cur = None
    x = y = 0
    i = 0
    n = len(cmds)
    while i < n:
        c = cmds[i]
        i += 1
        cid = c & 0x7
        count = c >> 3
        if cid == 7:                       # ClosePath
            if cur:
                cur.append(cur[0])
            continue
        if cid not in (1, 2):
            break
        for _ in range(count):
            if i + 1 >= n:
                break
            x += _zigzag(cmds[i])
            y += _zigzag(cmds[i + 1])
            i += 2
            if cid == 1:
                cur = [(x, y)]
                out.append(cur)
            else:
                if cur is None:
                    cur = [(x, y)]
                    out.append(cur)
                else:
                    cur.append((x, y))
    return [p for p in out if len(p) >= min_len]
