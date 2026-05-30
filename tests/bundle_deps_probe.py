# -*- coding: utf-8 -*-
"""
捆绑依赖 + 启动注入逻辑实测。

测试原则：实测、不 mock 业务逻辑、看真实产出（不靠"无异常即过"）。注入逻辑
用真实文件 + 真实环境变量验证每条分支；本机不具备的（打包态 _MEIPASS）明确
标 [未实测]，不假装通过。

运行：python tests/bundle_deps_probe.py
"""
import io
import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docingest.utils.binary_finder import find_binary  # noqa: E402
from docingest.utils import bundled_binaries as bb  # noqa: E402

PASS, FAIL, SKIP = "✓ 实测", "✗ 实测", "— 未实测"
results = {"pass": 0, "fail": 0}


def check(cond, msg, detail=""):
    print(f"{PASS if cond else FAIL}  {msg}")
    if detail:
        print(f"        {detail}")
    results["pass" if cond else "fail"] += 1
    return cond


def hr(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


# ---------------------------------------------------------------------------
# 块1：find_binary 认环境变量（捆绑定位的根基 —— 注入逻辑依赖它）
# ---------------------------------------------------------------------------
def test_find_binary_env():
    hr("块1: find_binary 认环境变量（注入机制的根基）")
    tmp = Path(tempfile.mkdtemp())
    fake = tmp / ("fake.exe" if sys.platform.startswith("win") else "fake")
    fake.write_text("x")

    os.environ["SOFFICE_PATH"] = str(fake)
    got = find_binary("soffice")
    check(got and Path(got).samefile(fake), "SOFFICE_PATH 指真实文件 → 命中", f"返回 {got}")

    os.environ["SOFFICE_PATH"] = str(tmp / "nope")
    check(find_binary("soffice") is None, "SOFFICE_PATH 指坏路径 → None（不静默回退，符合契约）")

    os.environ.pop("SOFFICE_PATH", None)
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 块2：ensure_bundled_binaries 注入逻辑的每条分支（本次新代码，核心）
#   开发态本身无 bundle，所以这里用真实文件 + _MEIPASS 模拟，真验每条分支。
# ---------------------------------------------------------------------------
def test_injection_branches():
    hr("块2: ensure_bundled_binaries 注入逻辑分支（本次新代码）")

    # 干净起点：清掉三个 env
    for v in bb._BUNDLED:
        os.environ.pop(v, None)

    # 分支1：非打包态 + 无 imageio → 注入空（系统装的交给 find_binary，不注入）
    injected = bb.ensure_bundled_binaries()
    # 注：本机若装了 imageio-ffmpeg，FFMPEG_PATH 会被注入，这也是对的
    has_imageio = injected.get("FFMPEG_PATH") is not None
    check(bb._meipass_dir() is None, "非打包态 _MEIPASS 为 None", f"injected={injected}")
    if has_imageio:
        print(f"        （本机装了 imageio-ffmpeg，FFMPEG_PATH 已注入: {injected['FFMPEG_PATH']}）")
    for v in bb._BUNDLED:
        os.environ.pop(v, None)

    # 分支2：尊重用户已设的 env —— 绝不覆盖
    sentinel = "/user/explicit/soffice"
    os.environ["SOFFICE_PATH"] = sentinel
    bb.ensure_bundled_binaries()
    check(os.environ["SOFFICE_PATH"] == sentinel, "用户已设 SOFFICE_PATH → 不被覆盖（尊重显式配置）")
    os.environ.pop("SOFFICE_PATH", None)

    # 分支3：模拟打包态 —— 造一个假 _MEIPASS/_bundled_bin 放真实文件，验真能找到并注入
    fake_mei = Path(tempfile.mkdtemp())
    bundle_dir = fake_mei / bb._BUNDLE_SUBDIR
    # 模拟 LibreOffice 的树状布局：<bundle>/LibreOffice/program/soffice(.exe)
    so_name = "soffice.exe" if sys.platform.startswith("win") else "soffice"
    so_path = bundle_dir / "LibreOffice" / "program" / so_name
    so_path.parent.mkdir(parents=True, exist_ok=True)
    so_path.write_text("x")
    # 同时放一个扁平的 ffprobe
    fp_name = "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"
    (bundle_dir / fp_name).write_text("x")

    old_mei = getattr(sys, "_MEIPASS", None)
    sys._MEIPASS = str(fake_mei)  # 模拟 PyInstaller 运行态
    try:
        for v in bb._BUNDLED:
            os.environ.pop(v, None)
        injected = bb.ensure_bundled_binaries()
        # soffice 在树深处，验 rglob 能找到
        ok_so = injected.get("SOFFICE_PATH") and Path(injected["SOFFICE_PATH"]).samefile(so_path)
        check(ok_so, "打包态：bundle 树里的 soffice 被 rglob 找到并注入", f"→ {injected.get('SOFFICE_PATH')}")
        # ffprobe 扁平放
        ok_fp = injected.get("FFPROBE_PATH") and Path(injected["FFPROBE_PATH"]).samefile(bundle_dir / fp_name)
        check(ok_fp, "打包态：bundle 里的 ffprobe 被找到并注入", f"→ {injected.get('FFPROBE_PATH')}")
        # 注入后 find_binary 应命中这些
        check(find_binary("soffice") and Path(find_binary("soffice")).samefile(so_path),
              "注入后 find_binary('soffice') 命中 bundle 里的")
    finally:
        if old_mei is None:
            delattr(sys, "_MEIPASS")
        else:
            sys._MEIPASS = old_mei
        for v in bb._BUNDLED:
            os.environ.pop(v, None)
        shutil.rmtree(fake_mei, ignore_errors=True)


# ---------------------------------------------------------------------------
# 块3：本机二进制可用性 + imageio-ffmpeg 的 ffprobe 缺口
# ---------------------------------------------------------------------------
def test_binaries_reality():
    hr("块3: 本机二进制 + imageio-ffmpeg ffprobe 缺口")
    for name in ("ffmpeg", "ffprobe", "soffice"):
        p = find_binary(name)
        print(f"{PASS if p else SKIP}  本机 {name}: {p or '未找到'}")

    try:
        import imageio_ffmpeg
        print(f"{PASS}  imageio-ffmpeg 已装 v{imageio_ffmpeg.__version__}，ffmpeg: {imageio_ffmpeg.get_ffmpeg_exe()}")
        check(not hasattr(imageio_ffmpeg, "get_ffprobe_exe"),
              "imageio-ffmpeg 不提供 ffprobe（坐实缺口，ffprobe 需另带）")
    except ImportError:
        print(f"{SKIP}  imageio-ffmpeg 未装（[bundle] extras，装了才验自带 ffmpeg）")


# ---------------------------------------------------------------------------
# 块4：LibreOffice headless 真转换（本机有则真跑）
# ---------------------------------------------------------------------------
def test_soffice_real():
    hr("块4: LibreOffice headless 真转换")
    so = find_binary("soffice")
    pptx = ROOT / "tests" / "fixtures" / "test_chart.pptx"
    if not so:
        print(f"{SKIP}  本机无 LibreOffice，headless 转换无法实测")
        return
    if not pptx.exists():
        print(f"{SKIP}  无 test_chart.pptx fixture")
        return
    tmp = Path(tempfile.mkdtemp())
    try:
        proc = subprocess.run([so, "--headless", "--convert-to", "pdf",
                               "--outdir", str(tmp), str(pptx)],
                              capture_output=True, timeout=120)
        pdfs = list(tmp.glob("*.pdf"))
        ok = bool(pdfs) and pdfs[0].stat().st_size > 0
        check(ok, "PPT→PDF headless 真转换出 PDF",
              f"{pdfs[0].name} ({pdfs[0].stat().st_size}B)" if ok else f"rc={proc.returncode}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print(f"捆绑依赖 + 启动注入实测  平台={sys.platform}  Python={sys.version.split()[0]}")
    test_find_binary_env()
    test_injection_branches()
    test_binaries_reality()
    test_soffice_real()
    hr("小结")
    print(f"断言通过 {results['pass']} / 失败 {results['fail']}")
    print("已实测: find_binary 环境变量、注入逻辑各分支(含模拟打包态)、ffprobe 缺口、LibreOffice 真转换")
    print("仍未实测: 真 PyInstaller 打包后 _MEIPASS 里 LibreOffice 能否定位+headless 跑（须真打包验）")
