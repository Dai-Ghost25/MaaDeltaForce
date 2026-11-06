# init.py
"""MDF 项目初始化脚本

零第三方依赖，只用 Python 标准库。
首次使用：python init.py

功能：
  1. 检查 Python 版本
  2. 检查/创建虚拟环境
  3. 检查/安装 Python 必要包
  4. 检查/下载 MaaFramework 二进制（GitHub API）
  5. 检查资源文件
"""
import sys
import subprocess
import shutil
import json
import platform
import importlib.util
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEPS_DIR = PROJECT_ROOT / "deps"

# import 名 -> 显示名
REQUIRED_PACKAGES = {
    "maa": "MaaFw (MaaFramework Python 绑定)",
    "cv2": "opencv-python (OpenCV)",
    "numpy": "numpy",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "requests": "requests",
}

# sys.platform / platform.machine() -> 资产名中的平台、架构片段
PLATFORM_TAG = {"win32": "win", "linux": "linux", "darwin": "macos"}
ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}

GITHUB_API = "https://api.github.com/repos/MaaXYZ/MaaFramework/releases/latest"


# ---------- 工具 ----------

def section(title: str):
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def ask_choice(prompt: str, options: list[tuple[str, str]], default: str = "0") -> str:
    for value, desc in options:
        marker = "*" if value == default else " "
        print(f"  {marker} [{value}] {desc}")
    try:
        ans = input(f"\n{prompt} (默认 {default}): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        sys.exit(130)
    return ans if ans else default


def yes_no(prompt: str, default: str = "n") -> bool:
    try:
        ans = input(f"{prompt} (y/N，默认 {default}): ").strip().lower() or default
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        sys.exit(130)
    return ans in ("y", "yes", "是")


def detect_platform() -> tuple[str, str] | None:
    """返回 (platform_tag, arch_tag) 或 None（不支持）"""
    plat = PLATFORM_TAG.get(sys.platform)
    arch_raw = platform.machine().lower()
    arch = ARCH_ALIASES.get(arch_raw)
    if not plat or not arch:
        return None
    return plat, arch


# ---------- 各项检查 ----------

def check_python():
    section("1. Python 环境")
    v = sys.version_info
    print(f"  ✅ Python: {v.major}.{v.minor}.{v.micro}")
    print(f"  可执行文件: {sys.executable}")
    print(f"  平台: {platform.platform()}")

    if v < (3, 10):
        print(f"\n  ⚠️  推荐 Python 3.10+，当前 {v.major}.{v.minor}")
        if not yes_no("  是否继续？"):
            sys.exit(1)


def check_venv():
    section("2. 虚拟环境")
    in_venv = (
        hasattr(sys, "real_prefix")
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    )
    if in_venv:
        print(f"  ✅ 已在虚拟环境中: {sys.prefix}")
        return

    print("  ⚠️  当前不在虚拟环境中（Linux/macOS 建议用 venv 隔离依赖）")

    venv_path = PROJECT_ROOT / ".venv"
    if venv_path.exists():
        print(f"\n  检测到已有 .venv: {venv_path}")
        if sys.platform == "win32":
            print(f"  激活: {venv_path}\\!Scripts\\activate.bat")
        else:
            print(f"  激活: source {venv_path}/bin/activate")
        return

    print()
    choice = ask_choice(
        "操作",
        [("0", "跳过（我已有环境，或 Windows 全局安装）"),
         ("1", "创建 .venv 并退出，让我激活后重新运行")],
        default="0",
    )
    if choice == "1":
        print(f"\n  创建虚拟环境: {venv_path}")
        try:
            subprocess.check_call([sys.executable, "-m", "venv", str(venv_path)])
        except subprocess.CalledProcessError as e:
            print(f"  ❌ 创建失败: {e}")
            return
        print("\n  ✅ 创建完成。请激活后重跑 init.py：")
        if sys.platform == "win32":
            print(f"     {venv_path}\\!Scripts\\activate.bat")
        else:
            print(f"     source {venv_path}/bin/activate")
        sys.exit(0)


def check_pip_deps():
    section("3. Python 必要包")
    missing = []
    for mod_name, desc in REQUIRED_PACKAGES.items():
        if importlib.util.find_spec(mod_name) is None:
            print(f"  ❌ {desc}  [{mod_name}]")
            missing.append(mod_name)
        else:
            print(f"  ✅ {desc}  [{mod_name}]")

    if not missing:
        print("\n  所有必要包已安装。")
        return

    print(f"\n  缺失 {len(missing)} 个包。")
    choice = ask_choice(
        "操作",
        [("0", "跳过（我自己处理）"),
         ("1", "用 pip install -r requirements.txt 安装")],
        default="0",
    )
    if choice == "1":
        req = PROJECT_ROOT / "requirements.txt"
        if not req.is_file():
            print(f"\n  ❌ 找不到 {req}")
            return
        print(f"\n  执行: {sys.executable} -m pip install -r {req}")
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(req)])
        print("  ✅ pip 安装完成" if rc == 0 else f"  ❌ pip 安装失败，退出码 {rc}")


def _find_existing_maafw() -> list[Path]:
    """在 deps/ 下查找 MaaFramework 二进制"""
    if not DEPS_DIR.exists():
        return []
    names = ("MaaFramework.dll", "libMaaFramework.so", "libMaaFramework.dylib")
    found = []
    for name in names:
        found.extend(DEPS_DIR.rglob(name))
    return found


def _download_latest_maa() -> bool:
    """通过 GitHub API 下载最新版 MaaFramework 到 deps/"""
    detected = detect_platform()
    if not detected:
        print(f"  ❌ 不支持的平台: {sys.platform}/{platform.machine()}")
        return False
    plat, arch = detected
    target_name = f"MAA-{plat}-{arch}.zip"
    print(f"  目标资产: {target_name}")

    # 1. 查询 release
    print(f"  查询: {GITHUB_API}")
    try:
        req = urllib.request.Request(
            GITHUB_API,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "mdf-init"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("  ❌ GitHub API 限流（60次/小时），请稍后再试或手动下载")
        else:
            print(f"  ❌ HTTP {e.code}: {e.reason}")
        return False
    except Exception as e:
        print(f"  ❌ 请求失败: {e}")
        return False

    tag = release.get("tag_name", "?")
    assets = release.get("assets", [])
    print(f"  最新版本: {tag}（共 {len(assets)} 个资产）")

    # 2. 匹配资产（精确名优先，再前缀匹配）
    asset = next((a for a in assets if a["name"] == target_name), None)
    if asset is None:
        asset = next((a for a in assets
                      if a["name"].startswith(f"MAA-{plat}-{arch}")
                      and a["name"].endswith(".zip")), None)

    if asset is None:
        print(f"  ❌ 未找到 {target_name}")
        print("  可用资产:")
        for a in assets:
            print(f"    - {a['name']}")
        return False

    url = asset["browser_download_url"]
    filename = asset["name"]
    print(f"  下载: {filename}")
    print(f"  URL: {url}")

    # 3. 下载
    DEPS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DEPS_DIR / filename
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, \
                open(zip_path, "wb") as f:
            shutil.copyfileobj(resp, f)
    except Exception as e:
        print(f"  ❌ 下载失败: {e}")
        zip_path.unlink(missing_ok=True)
        return False

    size_kb = zip_path.stat().st_size / 1024
    print(f"  下载完成: {size_kb:.1f} KB")

    # 4. 解压
    print(f"  解压到: {DEPS_DIR}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(DEPS_DIR)
    except Exception as e:
        print(f"  ❌ 解压失败: {e}")
        print(f"  ⚠️  下载的文件保留在: {zip_path}")
        # 打印前 200 字节，帮助诊断（是 HTML 还是 zip）
        try:
            head = zip_path.read_bytes()[:200]
            print(f"  文件头（前 200 字节）: {head!r}")
        except Exception:
            pass
        return False
    else:
        # 只有成功才删
        zip_path.unlink(missing_ok=True)

    # 5. 结构校验
    found = _find_existing_maafw()
    if found:
        for p in found:
            print(f"  ✅ {p.relative_to(PROJECT_ROOT)}")
        return True

    print("  ⚠️  解压完成，但 deps/ 下未找到 MaaFramework 二进制")
    print("  deps/ 顶层内容:")
    for p in sorted(DEPS_DIR.iterdir()):
        marker = "/" if p.is_dir() else ""
        print(f"    - {p.name}{marker}")
    print("  请确认 zip 内部结构，必要时手动移动到 deps/bin/ 下")
    return False


def check_maafw():
    section("4. MaaFramework 二进制")
    existing = _find_existing_maafw()
    if existing:
        for p in existing:
            print(f"  ✅ 检测到: {p.relative_to(PROJECT_ROOT)}")
        return

    print("  ❌ deps/ 下未找到 MaaFramework 二进制")
    if not DEPS_DIR.exists():
        print(f"  （deps/ 目录不存在: {DEPS_DIR}）")

    detected = detect_platform()
    if detected:
        print(f"  当前平台: {detected[0]}/{detected[1]}")
    else:
        print(f"  ⚠️  无法识别平台: {sys.platform}/{platform.machine()}")

    choice = ask_choice(
        "操作",
        [("0", "跳过（我已手动放置）"),
         ("1", "自动下载最新版（GitHub API）"),
         ("2", "手动下载（打印链接）")],
        default="0",
    )

    if choice == "0":
        return

    if choice == "1":
        if _download_latest_maa():
            return
        print("\n  自动下载失败。")
        if not yes_no("  是否查看手动下载链接？"):
            return

    # 手动分支
    print()
    print(f"  访问: https://github.com/MaaXYZ/MaaFramework/releases/latest")
    if detected:
        plat, arch = detected
        print(f"  下载 MAA-{plat}-{arch}.zip，解压到: {DEPS_DIR}")
    else:
        print(f"  选择对应平台的 MAA-*.zip，解压到: {DEPS_DIR}")


def check_resources():
    section("5. 资源文件")
    checks = [
        (PROJECT_ROOT / "assets" / "resource", "资源目录"),
        (PROJECT_ROOT / "config", "配置目录"),
        (PROJECT_ROOT / "assets" / "resource" / "model" / "ocr" / "det.onnx",
         "OCR det.onnx"),
        (PROJECT_ROOT / "assets" / "resource" / "model" / "ocr" / "rec.onnx",
         "OCR rec.onnx"),
    ]
    all_ok = True
    for path, desc in checks:
        if path.exists():
            print(f"  ✅ {desc}: {path.relative_to(PROJECT_ROOT)}")
        else:
            print(f"  ❌ {desc} 缺失: {path.relative_to(PROJECT_ROOT)}")
            all_ok = False
    if not all_ok:
        print("\n  ⚠️  部分资源缺失，请从仓库重新拉取或手动补充")


# ---------- 入口 ----------

def main():
    print("=" * 60)
    print("  MDF 初始化脚本")
    print("=" * 60)
    print(f"  项目根目录: {PROJECT_ROOT}")
    print(f"  Python:     {sys.version.split()[0]}")
    print(f"  平台:       {sys.platform}")

    check_python()
    check_venv()
    check_pip_deps()
    check_maafw()
    check_resources()

    print()
    print("=" * 60)
    print("  ✅ 初始化流程完成")
    print("=" * 60)
    print()
    print("下一步:")
    print("  python mdf.py list           # 查看可用任务")
    print("  python mdf.py run <任务名>   # 运行任务")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已取消")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ 初始化异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)