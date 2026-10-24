"""依次跑三个自检脚本，最后给一句结论。全部不需要联网。

用法（双击 运行测试.bat 效果一样）：
    runtime/python.exe run_tests.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TESTS = [
    ("转换链路：GeoJSON → DXF", "selftest.py"),
    ("数据解析：裁剪 / 拼环 / 图层归类", "test_parse.py"),
    ("网络层：代理识别 + 服务器繁忙自动降级", "test_proxy.py"),
    ("矢量瓦片通道：解码 / 坐标 / 图层 / 路名", "test_tiles.py"),
    ("标注层：建筑名 / 图层 / 文字坐标", "test_labels.py"),
    ("道路轮廓：路口融合 / 院落空洞 / 桥梁压过", "test_outline.py"),
    ("卫星图图层：瓦片定位 / 坐标一一对应 / 1:1 精度", "test_satellite.py"),
]


def line(char="-"):
    print(char * 58)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    print("=" * 58)
    print("  工具自检（全部不需要联网）")
    print("=" * 58)

    results = []
    for i, (title, script) in enumerate(TESTS, 1):
        print()
        print(f"[{i}/{len(TESTS)}] {title}")
        line()
        try:
            code = subprocess.run([sys.executable, "-u", str(ROOT / script)],
                                  cwd=str(ROOT)).returncode
        except Exception as e:  # noqa: BLE001
            print(f"  无法运行 {script}：{e}")
            code = 1
        results.append((title, code == 0))

    print()
    print("=" * 58)
    for title, good in results:
        print(("  [通过] " if good else "  [失败] ") + title)
    print("=" * 58)
    print()

    if all(good for _, good in results):
        print("结论：工具本身没问题。")
        print("抓取有两条路可走：")
        print("  1) 有代理时：走 Overpass / OSM 官方接口（数据最全）")
        print("  2) 没代理时：自动走「公共矢量瓦片」，国内可直连，不需要翻墙")
        print("如果两条路都失败，先看页面上的「测试数据源连通性」给出的结论。")
        return 0

    print("结论：有检查没通过。请把上面的输出整段发给技术支持。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
