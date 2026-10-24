"""几何小工具：矩形裁剪（线 / 面）、把零散线段拼成闭合环。

坐标系无关，全部用 (x, y) 元组序列。
"""


def center(pts):
    """一组点的大致中心（取平均，够用）。"""
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _clip_segment(x0, y0, x1, y1, rect):
    """Liang-Barsky 线段裁剪，返回裁剪后的线段或 None。"""
    minx, miny, maxx, maxy = rect
    dx = x1 - x0
    dy = y1 - y0
    p = [-dx, dx, -dy, dy]
    q = [x0 - minx, maxx - x0, y0 - miny, maxy - y0]
    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if pi == 0:
            if qi < 0:
                return None
        else:
            t = qi / pi
            if pi < 0:
                if t > u2:
                    return None
                if t > u1:
                    u1 = t
            else:
                if t < u1:
                    return None
                if t < u2:
                    u2 = t
    if u1 > u2:
        return None
    return ((x0 + u1 * dx, y0 + u1 * dy), (x0 + u2 * dx, y0 + u2 * dy))


def clip_line(points, rect, eps=1e-9):
    """把折线裁剪到矩形内，返回若干条折线（保持连通性）。"""
    out_lines = []
    cur = []
    for i in range(len(points) - 1):
        seg = _clip_segment(points[i][0], points[i][1],
                            points[i + 1][0], points[i + 1][1], rect)
        if seg is None:
            if len(cur) >= 2:
                out_lines.append(cur)
            cur = []
            continue
        a, b = seg
        if not cur or abs(cur[-1][0] - a[0]) > eps or abs(cur[-1][1] - a[1]) > eps:
            if len(cur) >= 2:
                out_lines.append(cur)
            cur = [a]
        cur.append(b)
        # 如果这一段被截断（终点不等于原终点），说明出去过，断开
        if abs(b[0] - points[i + 1][0]) > eps or abs(b[1] - points[i + 1][1]) > eps:
            if len(cur) >= 2:
                out_lines.append(cur)
            cur = []
    if len(cur) >= 2:
        out_lines.append(cur)
    return out_lines


def _clip_poly_edge(points, inside, intersect):
    """Sutherland-Hodgman 单边裁剪。"""
    out = []
    n = len(points)
    for i in range(n):
        cur = points[i]
        prev = points[i - 1]
        cur_in = inside(cur)
        prev_in = inside(prev)
        if cur_in:
            if not prev_in:
                out.append(intersect(prev, cur))
            out.append(cur)
        elif prev_in:
            out.append(intersect(prev, cur))
    return out


def clip_polygon(ring, rect):
    """Sutherland-Hodgman 多边形裁剪，返回裁剪后的环（可能为空）。"""
    minx, miny, maxx, maxy = rect
    pts = list(ring)
    if len(pts) < 3:
        return []

    def inside_left(p):  return p[0] >= minx
    def inside_right(p): return p[0] <= maxx
    def inside_bottom(p): return p[1] >= miny
    def inside_top(p): return p[1] <= maxy

    def inter_x(xv, p1, p2):
        t = (xv - p1[0]) / (p2[0] - p1[0])
        return (xv, p1[1] + t * (p2[1] - p1[1]))

    def inter_y(yv, p1, p2):
        t = (yv - p1[1]) / (p2[1] - p1[1])
        return (p1[0] + t * (p2[0] - p1[0]), yv)

    for inside, inter in (
        (inside_left, lambda a, b: inter_x(minx, a, b)),
        (inside_right, lambda a, b: inter_x(maxx, a, b)),
        (inside_bottom, lambda a, b: inter_y(miny, a, b)),
        (inside_top, lambda a, b: inter_y(maxy, a, b)),
    ):
        if not pts:
            return []
        pts = _clip_poly_edge(pts, inside, inter)
    if len(pts) < 3:
        return []
    # 去掉连续重复点
    cleaned = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - cleaned[-1][0]) > 1e-9 or abs(p[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(p)
    return cleaned if len(cleaned) >= 3 else []


def stitch_rings(ways, precision=7):
    """把多条首尾相接的 way 拼成闭合环（用于 OSM relation 的多边形）。"""
    pool = [[(round(p[0], precision), round(p[1], precision)) for p in w]
            for w in ways if len(w) >= 2]
    rings = []
    while pool:
        cur = pool.pop(0)
        changed = True
        guard = 0
        while changed and guard < 10000:
            changed = False
            guard += 1
            if cur[0] == cur[-1]:
                break
            for i, w in enumerate(pool):
                if w[0] == cur[-1]:
                    cur.extend(w[1:]); pool.pop(i); changed = True; break
                if w[-1] == cur[-1]:
                    cur.extend(list(reversed(w))[1:]); pool.pop(i); changed = True; break
                if w[-1] == cur[0]:
                    cur = w[:-1] + cur; pool.pop(i); changed = True; break
                if w[0] == cur[0]:
                    cur = list(reversed(w))[:-1] + cur; pool.pop(i); changed = True; break
        if len(cur) >= 4:
            rings.append(cur)
    return rings
