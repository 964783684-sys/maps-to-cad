"""命令行批量转换：GeoJSON -> DXF

用法：
    runtime\\python.exe cli.py output\\map_20261023_120000.geojson
    runtime\\python.exe cli.py 某个文件夹          （批量转文件夹里所有 geojson）
    runtime\\python.exe cli.py a.geojson --text-height 3 --road-mode line --label all
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from map2cad.dxf_export import export_dxf  # noqa: E402


def convert_one(src: Path, args):
    try:
        geojson = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  × {src.name}: 读不了（{e}）")
        return False
    if geojson.get("type") != "FeatureCollection":
        print(f"  × {src.name}: 不是 GeoJSON FeatureCollection")
        return False

    out = Path(args.out) if args.out else src.with_suffix(".dxf")
    try:
        res = export_dxf(
            geojson, str(out),
            text_height=args.text_height,
            road_mode=args.road_mode,
            label_mode=args.label,
            info_text=not args.no_info,
            road_outline=not args.no_outline,
            road_fill=args.fill,
            satellite=not args.no_satellite,
            log=None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  × {src.name}: 转换失败（{e}）")
        return False
    print(f"  √ {src.name} -> {out.name}  "
          f"({res['polylines']} 条多段线, {res['texts']} 处注记, "
          f"{res['width_m']}×{res['height_m']} m)"
          + (f" + 卫星图 {res['satellite']['px'][0]}×{res['satellite']['px'][1]}"
             if res.get("satellite") else ""))
    return True


def main():
    ap = argparse.ArgumentParser(description="GeoJSON 转 DXF（CAD 图纸）")
    ap.add_argument("src", help="geojson 文件，或包含 geojson 的文件夹")
    ap.add_argument("--out", default=None, help="输出 dxf 路径（仅在单文件时有效）")
    ap.add_argument("--text-height", type=float, default=2.5, help="注记文字高度（米），默认 2.5")
    ap.add_argument("--road-mode", choices=["band", "line"], default="band",
                    help="band=按实际宽度（默认），line=只画中心线")
    ap.add_argument("--label", choices=["road", "all", "none"], default="road",
                    help="标注内容：all=建筑名+路名（默认），road=只路名，none=不要")
    ap.add_argument("--no-info", action="store_true", help="不生成左下角的信息文字")
    ap.add_argument("--no-outline", action="store_true",
                    help="不生成「道路轮廓」图层（默认生成：路口自动融合、桥梁压过下方道路）")
    ap.add_argument("--fill", action="store_true",
                    help="道路按等级做实体填充（默认不填：填充会盖住道路边线）")
    ap.add_argument("--no-satellite", action="store_true",
                    help="不抓卫星图底图（默认抓：会多存一张 xxx_卫星图.jpg，要和 dxf 放一起）")
    args = ap.parse_args()

    src = Path(args.src)
    if src.is_dir():
        files = sorted(list(src.glob("*.geojson")) + list(src.glob("*.json")))
        if not files:
            print("这个文件夹里没有 .geojson 文件")
            return 1
        print(f"批量转换 {len(files)} 个文件…")
        ok = sum(convert_one(f, args) for f in files)
        print(f"\n完成 {ok}/{len(files)}")
        return 0 if ok == len(files) else 1

    if not src.exists():
        print(f"找不到文件：{src}")
        return 1
    print("转换中…")
    return 0 if convert_one(src, args) else 1


if __name__ == "__main__":
    sys.exit(main())
