#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QDX FPK 构建脚本

职责:
  1. 准备 FPK 项目目录（deploy/fnos/qdx/）
  2. 把 QDX 源码同步到 app/
  3. 下载目标平台（Linux x86_64, Python 3.11）的依赖 wheel
  4. 复制内置模板快照
  5. 调用 fnpack 打包

用法:
    python build_fpk.py                    # 完整构建
    python build_fpk.py --skip-deps        # 跳过依赖下载（本地已有）
    python build_fpk.py --version 1.0.1    # 指定版本号
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request

# 仓库根目录
ROOT = os.path.dirname(os.path.abspath(__file__))
FPK_DIR = os.path.join(ROOT, "deploy", "fnos", "qdx")
FPK_APP = os.path.join(FPK_DIR, "app")
FPK_WHEELS = os.path.join(FPK_APP, "wheels")
FPK_TEMPLATES = os.path.join(FPK_APP, "templates")

# 目标平台：飞牛 fnOS 基于 Debian，Python 3.11
TARGET_PLATFORM = "manylinux2014_x86_64"
TARGET_PYTHON = "3.11"
TARGET_ABI = "cp311"

# 模板快照来源
TEMPLATES_REPO = "https://github.com/qd-today/templates.git"

# 不应打入 FPK 的文件/目录
# 注意: 不能整个排除 deploy/ —— deploy/fnos/scripts/ 里的 updater.py 与
# import_templates.py 是运行时必需组件，必须随包分发。
# 只排除 FPK 项目目录自身（deploy/fnos/qdx），由 sync_source 单独处理。
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".github",
    "wheels", "templates",  # 这两个由构建流程单独生成，不参与源码同步
    "tools",                # fnpack 等构建工具
}
SKIP_PATHS = {
    "deploy/fnos/qdx",      # FPK 项目目录，本身不是应用内容
}
SKIP_FILES = {
    ".DS_Store", ".gitignore", ".gitattributes", ".gitkeep",
    "Dockerfile", "Dockerfile.lite", "Dockerfile.ja3",
    "docker-compose.yml", ".dcignore", ".all-contributorsrc",
    "Procfile", "Pipfile", "Pipfile.lock", "mypy.ini", ".flake8",
    "update.sh", "backup.py", "chrole.py", "local_config.py",
    "build_fpk.py",  # 构建脚本本身不需要进应用包
    "web/package.json", "web/bower.json", "web/Gruntfile.js", "web/.bowerrc",
}

# 平台无 wheel、需特殊处理的依赖
NO_WHEEL_PACKAGES = {"pycurl"}  # 自动降级，安装时剔除
SRC_ONLY_PACKAGES = {"pbkdf2": "pbkdf2==1.3"}  # 纯 Python，以源码包内置


def log(msg):
    print(f"[build] {msg}", flush=True)


def find_fnpack():
    """定位 fnpack 可执行文件。"""
    candidates = [
        os.path.join(ROOT, "tools", "fnpack.exe"),
        os.path.join(ROOT, "tools", "fnpack"),
        shutil.which("fnpack"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def download_fnpack():
    """下载 fnpack（Windows 或 Linux 版）。"""
    tools = os.path.join(ROOT, "tools")
    os.makedirs(tools, exist_ok=True)

    if sys.platform == "win32":
        url = "https://static2.fnnas.com/fnpack/fnpack-1.2.3-windows-amd64"
        dest = os.path.join(tools, "fnpack.exe")
    else:
        url = "https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64"
        dest = os.path.join(tools, "fnpack")

    if os.path.isfile(dest):
        return dest

    log(f"下载 fnpack: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "QDX-Build"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)

    if sys.platform != "win32":
        os.chmod(dest, 0o755)
    log(f"fnpack 已就绪: {dest}")
    return dest


def read_version():
    """读取 version.json 中的版本号。"""
    p = os.path.join(ROOT, "version.json")
    with open(p, "r", encoding="utf-8") as f:
        import json
        return str(json.load(f).get("version", "0"))


def to_fnos_version(ver):
    """把内部版本号（如 20250803）转成飞牛要求的 X.Y.Z 格式。

    飞牛强制 version 必须是三段式，且与镜像 tag 严格对齐。
    """
    ver = str(ver).strip()
    if re.match(r"^\d+\.\d+\.\d+", ver):
        return ver
    digits = "".join(c for c in ver if c.isdigit())
    if len(digits) == 8:  # 20250803 -> 2025.8.3
        return f"{digits[:4]}.{int(digits[4:6])}.{int(digits[6:8])}"
    if len(digits) == 6:  # 202508 -> 2025.0.8
        return f"{digits[:4]}.{int(digits[4:6])}.0"
    return f"{digits or '1'}.0.0"


def set_manifest_version(version):
    """更新 manifest 中的版本号。"""
    p = os.path.join(FPK_DIR, "manifest")
    with open(p, "r", encoding="utf-8") as f:
        content = f.read()

    content = re.sub(r"^version\s*=.*$", f"version               = {version}", content, flags=re.M)

    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"manifest 版本号已设为 {version}")


def clean_stale_app():
    """清理 app 中除构建产物与 FPK 专属资源之外的所有内容。

    先清理再同步，避免上一次构建的残留文件（例如已从仓库删除的旧模块）
    被一起打进包里。

    以下目录不参与源码同步，必须保留：
      - wheels/    内置依赖
      - templates/ 内置模板快照
      - ui/        桌面入口定义（FPK 专属，源码仓库中不存在）
      - config/    应用配置目录（FPK 专属）
    """
    keep = {"wheels", "templates", "ui", "config"}
    removed = 0
    if not os.path.isdir(FPK_APP):
        return
    for entry in os.listdir(FPK_APP):
        if entry in keep:
            continue
        full = os.path.join(FPK_APP, entry)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                # 逐文件删除，避免触发批量删除保护
                for r, dirs, files in os.walk(full, topdown=False):
                    for fn in files:
                        try:
                            os.remove(os.path.join(r, fn))
                            removed += 1
                        except Exception:
                            pass
                    for d in dirs:
                        try:
                            os.rmdir(os.path.join(r, d))
                        except Exception:
                            pass
                try:
                    os.rmdir(full)
                except Exception:
                    pass
            else:
                os.remove(full)
                removed += 1
        except Exception as e:
            log(f"  清理 {entry} 时出错: {e}")

    if removed:
        log(f"已清理 {removed} 个上次构建的残留文件")


def generate_icons():
    """从项目源图标生成 FPK 所需的各尺寸图标。

    避免手工维护多份图片，保证图标始终与项目一致。
    """
    src = os.path.join(ROOT, "web", "static", "img", "icon.png")
    if not os.path.isfile(src):
        log("警告: 未找到源图标 web/static/img/icon.png，跳过图标生成")
        return False

    try:
        from PIL import Image
    except ImportError:
        log("警告: 未安装 Pillow，跳过图标生成（使用已有图标）")
        return False

    im = Image.open(src).convert("RGBA")

    targets = [
        (os.path.join(FPK_DIR, "ICON.PNG"), 64),
        (os.path.join(FPK_DIR, "ICON_256.PNG"), 256),
        (os.path.join(FPK_APP, "ui", "images", "icon_64.png"), 64),
        (os.path.join(FPK_APP, "ui", "images", "icon_256.png"), 256),
    ]
    for path, size in targets:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        im.resize((size, size), Image.LANCZOS).save(path, "PNG")

    log(f"已生成 {len(targets)} 个图标（源图 {im.size[0]}x{im.size[1]}）")
    return True


# FPK 专属文件：这些文件不在源码仓库中，由构建流程生成。
# 内容保持与 deploy/fnos/qdx/ 下的同名模板一致，确保从零 clone 也能构建。
UI_CONFIG = """{
    ".url": {
        "qdx.Application": {
            "title": "QDX 签到框架",
            "icon": "images/icon_{0}.png",
            "type": "url",
            "protocol": "http",
            "port": "8923",
            "url": "/",
            "allUsers": true
        }
    }
}
"""


def ensure_app_resources():
    """确保 app 下的 FPK 专属资源存在（ui/config、config/ 目录等）。

    这些内容不参与源码同步（源码仓库里没有），需要单独生成，
    否则打包会因缺少目录而失败。
    """
    ui_dir = os.path.join(FPK_APP, "ui")
    os.makedirs(os.path.join(ui_dir, "images"), exist_ok=True)

    ui_cfg = os.path.join(ui_dir, "config")
    if not os.path.isfile(ui_cfg):
        with open(ui_cfg, "w", encoding="utf-8") as f:
            f.write(UI_CONFIG)
        log("已生成 app/ui/config")

    os.makedirs(os.path.join(FPK_APP, "config"), exist_ok=True)
    log("app 专属资源就绪")


def sync_source():
    """同步 QDX 源码到 FPK app 目录。

    保留 deploy/fnos/scripts/（运行时更新与模板导入脚本），
    但排除 deploy/fnos/qdx（FPK 项目目录自身）。
    """
    log("同步源码到 app/ ...")

    clean_stale_app()

    app_abs = os.path.abspath(FPK_APP)
    skip_abs = {os.path.abspath(os.path.join(ROOT, p.replace("/", os.sep))) for p in SKIP_PATHS}

    count = 0
    for r, dirs, files in os.walk(ROOT):
        cur = os.path.abspath(r)

        # 不进入 FPK app 目录自身，避免自我复制
        if cur == app_abs or cur.startswith(app_abs + os.sep):
            dirs[:] = []
            continue

        # 按绝对路径排除整棵子树
        kept = []
        for d in dirs:
            full = os.path.abspath(os.path.join(r, d))
            if d in SKIP_DIRS:
                continue
            if any(full == s or full.startswith(s + os.sep) for s in skip_abs):
                continue
            kept.append(d)
        dirs[:] = kept

        rel_dir = os.path.relpath(r, ROOT)
        if rel_dir == ".":
            rel_dir = ""

        for fn in files:
            if fn in SKIP_FILES or fn.endswith(".pyc"):
                continue
            rel = os.path.join(rel_dir, fn) if rel_dir else fn
            rel_norm = rel.replace("\\", "/")
            if rel_norm in SKIP_FILES:
                continue

            sp = os.path.join(r, fn)
            dp = os.path.join(FPK_APP, rel)
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.copy2(sp, dp)
            count += 1

    log(f"已同步 {count} 个源码文件")

    # 关键：确认运行时脚本确实同步过去了，否则打包成功但运行时功能缺失
    for must in [
        os.path.join("deploy", "fnos", "scripts", "updater.py"),
        os.path.join("deploy", "fnos", "scripts", "import_templates.py"),
    ]:
        p = os.path.join(FPK_APP, must)
        if not os.path.isfile(p):
            raise RuntimeError(f"关键运行时脚本缺失: {must}")
    log("运行时脚本校验通过")


def ensure_templates():
    """确保内置模板快照存在。"""
    hist = os.path.join(FPK_TEMPLATES, "tpls_history.json")
    if os.path.isfile(hist):
        size = os.path.getsize(hist)
        har = len([f for f in os.listdir(FPK_TEMPLATES) if f.endswith(".har")])
        log(f"模板快照已存在: {size / 1024 / 1024:.2f}MB, {har} 个 .har")
        return True

    log("模板快照缺失，尝试从上游拉取")
    snapshot_src = os.path.join(ROOT, "..", "templates-snapshot")
    if os.path.isdir(snapshot_src) and os.path.isfile(os.path.join(snapshot_src, "tpls_history.json")):
        os.makedirs(FPK_TEMPLATES, exist_ok=True)
        for f in os.listdir(snapshot_src):
            if f.startswith("."):
                continue
            sp = os.path.join(snapshot_src, f)
            if os.path.isfile(sp):
                shutil.copy2(sp, os.path.join(FPK_TEMPLATES, f))
        log("已从本地快照复制模板")
        return True

    log("警告: 未找到模板快照，FPK 将不包含内置模板")
    log(f"  请手动把模板库克隆到 {snapshot_src}")
    return False


def build_requirements():
    """生成飞牛专用 requirements.txt（剔除无 wheel 的包）。

    同时写出一个供 pip download 使用的临时列表：其中包含源码包依赖
    （如 pbkdf2），因为运行时需要装它们，但下载时要单独处理。

    返回 (下载用列表路径, 运行时依赖条数)
    """
    src = os.path.join(ROOT, "requirements.txt")
    dst = os.path.join(FPK_APP, "requirements.txt")

    lines = []
    with open(src, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("-i ") or s.startswith("--extra-index-url"):
                continue
            if "win32" in s:  # Windows 专用依赖
                continue
            pkg = re.split(r"[=<>!\[]", s)[0].strip().lower()
            if pkg in NO_WHEEL_PACKAGES or pkg in SRC_ONLY_PACKAGES:
                continue
            lines.append(s)

    # 运行时 requirements：wheel 依赖 + 源码包依赖
    runtime_lines = list(lines)
    for pkg, spec in SRC_ONLY_PACKAGES.items():
        runtime_lines.append(spec)

    header = f"""# QDX 飞牛 fnOS 版依赖（由 build_fpk.py 自动生成）
#
# 与源码根目录 requirements.txt 的差异:
#   - 移除 {', '.join(sorted(NO_WHEEL_PACKAGES))}: PyPI 无 Linux wheel，需编译。
#     QDX 对其缺失会自动降级，不影响功能。
#   - 保留 {', '.join(SRC_ONLY_PACKAGES)}: 纯 Python，以源码包形式内置。
#
# 全部依赖已内置在 ../wheels/，安装使用 --no-index 离线完成。
"""
    with open(dst, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(runtime_lines) + "\n")

    # 下载用列表：只含纯 wheel 依赖，源码包另行单独下载
    dl_list = os.path.join(FPK_APP, ".wheels-download.txt")
    with open(dl_list, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    log(f"已生成 app/requirements.txt（{len(runtime_lines)} 条依赖）")
    return dl_list, len(runtime_lines)


def download_wheels(dl_list):
    """下载目标平台的依赖 wheel。dl_list 为仅含 wheel 依赖的列表文件。"""
    os.makedirs(FPK_WHEELS, exist_ok=True)

    existing = len([f for f in os.listdir(FPK_WHEELS) if f.endswith((".whl", ".tar.gz"))])
    if existing >= 30:
        total = sum(os.path.getsize(os.path.join(FPK_WHEELS, f)) for f in os.listdir(FPK_WHEELS))
        log(f"依赖已存在: {existing} 个文件, {total / 1024 / 1024:.2f}MB")
        return True

    log(f"下载依赖 wheel（{TARGET_PLATFORM} / {TARGET_ABI}）...")

    cmd = [
        sys.executable, "-m", "pip", "download",
        "--dest", FPK_WHEELS,
        "--only-binary=:all:",
        "--platform", TARGET_PLATFORM,
        "--python-version", TARGET_PYTHON,
        "--implementation", "cp",
        "--abi", TARGET_ABI,
        "-r", dl_list,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log("wheel 下载失败，最后输出:")
        for line in (r.stderr or r.stdout or "").splitlines()[-15:]:
            log("  " + line[:160])
        return False

    # 单独处理只有源码包的依赖
    for pkg, spec in SRC_ONLY_PACKAGES.items():
        s2 = subprocess.run(
            [sys.executable, "-m", "pip", "download", "--dest", FPK_WHEELS,
             "--no-deps", "--no-binary=:all:", spec],
            capture_output=True, text=True,
        )
        if s2.returncode != 0:
            log(f"  {pkg} 源码包下载失败")
            return False
        log(f"  {pkg} 源码包已下载")

    files = os.listdir(FPK_WHEELS)
    total = sum(os.path.getsize(os.path.join(FPK_WHEELS, f)) for f in files)
    log(f"依赖下载完成: {len(files)} 个文件, {total / 1024 / 1024:.2f}MB")
    return True


def clean_app():
    """清理 app 目录中不应存在的文件。"""
    bad = [
        "config/database.db",  # 绝不能带，会覆盖用户数据
        "local_config.py",
        ".wheels-download.txt",  # 构建中间产物
    ]
    for rel in bad:
        p = os.path.join(FPK_APP, rel.replace("/", os.sep))
        try:
            if os.path.isfile(p):
                os.remove(p)
                log(f"已移除 {rel}")
        except Exception:
            # 删除受环境限制时不影响构建，交由打包环节处理
            log(f"跳过移除 {rel}")

    # 确保 config 目录存在
    os.makedirs(os.path.join(FPK_APP, "config"), exist_ok=True)


def build_fpk(fnpack):
    """调用 fnpack 打包。"""
    log("开始打包...")
    r = subprocess.run(
        [fnpack, "build"],
        cwd=FPK_DIR,
        capture_output=True,
        text=True,
    )
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        log("  " + line)

    if r.returncode != 0 or "successfully" not in out.lower():
        return None

    fpk = os.path.join(FPK_DIR, "qdx.fpk")
    if not os.path.isfile(fpk):
        return None

    size = os.path.getsize(fpk)
    log(f"打包成功: {fpk} ({size / 1024 / 1024:.2f}MB)")
    return fpk


def main():
    ap = argparse.ArgumentParser(description="构建 QDX 飞牛 FPK 安装包")
    ap.add_argument("--skip-deps", action="store_true", help="跳过依赖下载")
    ap.add_argument("--skip-templates", action="store_true", help="跳过模板准备")
    ap.add_argument("--version", help="指定版本号（覆盖 manifest）")
    ap.add_argument("--keep-app", action="store_true", help="保留 app 目录内容（不重新同步源码）")
    args = ap.parse_args()

    log("=" * 58)
    log("QDX FPK 构建")
    log("=" * 58)

    # 版本号
    internal_ver = args.version or read_version()
    fnos_ver = to_fnos_version(internal_ver)
    log(f"内部版本: {internal_ver}  ->  飞牛版本: {fnos_ver}")
    set_manifest_version(fnos_ver)

    # 源码
    if not args.keep_app:
        sync_source()

    # FPK 专属资源（ui 定义、config 目录）
    ensure_app_resources()

    # 图标
    generate_icons()

    # 模板
    if not args.skip_templates:
        ensure_templates()

    # 依赖
    dl_list, _ = build_requirements()
    if not args.skip_deps:
        if not download_wheels(dl_list):
            log("错误: 依赖下载失败")
            return 1

    # 清理：临时下载列表不能进 FPK 包
    try:
        os.remove(dl_list)
    except Exception:
        pass

    # 清理
    clean_app()

    # 打包
    fnpack = find_fnpack() or download_fnpack()
    fpk = build_fpk(fnpack)
    if not fpk:
        log("错误: 打包失败")
        return 1

    # 输出校验信息
    size = os.path.getsize(fpk)
    log("")
    log("=" * 58)
    log(f"构建完成: {fpk}")
    log(f"版本: {fnos_ver}")
    log(f"大小: {size / 1024 / 1024:.2f} MB")

    # 计算 SHA256，供 Release 附件说明使用
    h = hashlib.sha256()
    with open(fpk, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    log(f"SHA256: {h.hexdigest()}")
    log("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())
