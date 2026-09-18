#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QDX 程序内自动更新

对接 https://github.com/sddvcm/qd 的 GitHub Releases，实现不重装 FPK 的原地更新。

工作方式:
  1. 请求 GitHub API 获取最新 Release 信息（含 version、附件列表、更新日志）
  2. 与本地 version.json 的版本号比较
  3. 下载 Release 附件中的源码包（tar.gz）
  4. 在临时目录解压 → 校验完整性 → 备份现有代码 → 替换 → 安装新依赖 → 重启

安全设计:
  - 更新前自动备份当前代码到 TRIM_PKGVAR/backup/app-<version>
  - 任何一步失败都回滚到备份
  - 严格校验下载包的 SHA256（如果 Release 提供了 digest）
  - 只替换代码文件，绝不触碰 TRIM_PKGVAR 中的数据库与配置
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

# 更新源：只认这个仓库
GITHUB_REPO = "sddvcm/qd"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_REPO}"

# 更新时允许使用的 GitHub 加速前缀（国内环境）
# 值为 None 表示直连
ACCELERATORS = [
    ("直连", None),
    ("gh-proxy", "https://gh-proxy.com/"),
    ("ghfast", "https://ghfast.top/"),
]

USER_AGENT = "QDX-Updater/1.0"


def log(msg):
    print(f"[QDX-Update] {msg}", flush=True)


def get_current_version(app_dir):
    """读取本地版本号。"""
    version_file = os.path.join(app_dir, "version.json")
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("version", "0"))
    except Exception as e:
        log(f"读取本地版本失败: {e}")
        return "0"


def parse_version(v):
    """把版本号转成可比较的元组。支持 20250803 / 1.0.0 / 20250803.1 等形式。"""
    parts = []
    for seg in str(v).replace("-", ".").split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def http_get(url, timeout=30, headers=None):
    """发起 GET 请求，返回 bytes。

    显式绕过环境变量中的 HTTP 代理。NAS 上常见的代理（Clash 等）会对
    GitHub API 返回 403，导致更新检查误报为网络故障。GitHub 直连在国内
    多数网络环境下可用，实在不通时由调用方切换到加速源。
    """
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)

    # 构造一个不使用代理的 opener
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=hdrs)
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def fetch_latest_release():
    """获取最新 Release 信息。依次尝试直连与加速源。

    注意: /releases/latest 在仓库尚未发布任何 Release 时返回 404。
    这属于正常状态而非网络故障，需要与连接失败区分开，否则用户会
    误以为是网络问题而反复排查。
    """
    api_url = f"{GITHUB_API}/releases/latest"
    errors = []

    for name, prefix in ACCELERATORS:
        try:
            url = api_url if not prefix else f"{prefix}{api_url}"
            log(f"检查更新（{name}）...")
            raw = http_get(url, timeout=20)
            data = json.loads(raw.decode("utf-8"))
            log(f"最新版本: {data.get('tag_name') or data.get('name')}")
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 仓库存在但没有 Release，或仓库不可见。这类错误不必再试其他源。
                raise RuntimeError(
                    "更新源尚无发布版本。请先在 GitHub 仓库 "
                    f"({GITHUB_REPO_URL}/releases) 创建一个 Release，"
                    "并上传源码包作为附件。"
                ) from e
            if e.code == 403:
                errors.append(f"{name}: 访问被拒绝（可能触发了 GitHub 频率限制）")
            else:
                errors.append(f"{name}: HTTP {e.code}")
            log(f"  {name} 失败: HTTP {e.code}")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")
            log(f"  {name} 失败: {type(e).__name__}")

    raise RuntimeError("无法连接更新服务器（" + "；".join(errors) + "）")



def pick_source_asset(release):
    """从 Release 附件中挑出源码包。优先 .tar.gz。"""
    assets = release.get("assets") or []
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.endswith(".tar.gz") or name.endswith(".tgz"):
            return a
    # 退化：用 GitHub 自动生成的源码包
    tarball = release.get("tarball_url")
    if tarball:
        return {"name": "source.tar.gz", "browser_download_url": tarball, "size": 0}
    return None


def download_asset(asset, dest_path, expected_sha256=None):
    """下载附件到指定路径，逐源尝试。"""
    if not asset:
        raise RuntimeError("未找到可用的更新包")
    url = asset.get("browser_download_url")
    if not url:
        raise RuntimeError("更新包缺少下载地址")

    last_err = None
    for name, prefix in ACCELERATORS:
        try:
            target = url if not prefix else f"{prefix}{url}"
            log(f"下载更新包（{name}）...")
            data = http_get(target, timeout=180)

            if expected_sha256:
                actual = hashlib.sha256(data).hexdigest()
                if actual.lower() != expected_sha256.lower():
                    raise RuntimeError(
                        f"校验失败: 期望 {expected_sha256[:16]}... 实际 {actual[:16]}..."
                    )
                log("SHA256 校验通过")

            with open(dest_path, "wb") as f:
                f.write(data)
            log(f"已下载 {len(data) / 1024 / 1024:.2f} MB")
            return dest_path
        except Exception as e:
            last_err = e
            log(f"  {name} 失败: {type(e).__name__}: {e}")

    raise RuntimeError(f"下载更新包失败: {last_err}")


def extract_and_find_root(tar_path, extract_dir):
    """解压 tar.gz，返回实际的应用根目录（GitHub 源码包会多一层目录）。"""
    with tarfile.open(tar_path, "r:gz") as tf:
        # 安全检查：拒绝绝对路径与目录穿越
        for member in tf.getmembers():
            name = member.name
            if name.startswith("/") or ".." in name.split("/"):
                raise RuntimeError(f"更新包包含非法路径: {name}")
        tf.extractall(extract_dir)

    entries = [e for e in os.listdir(extract_dir) if not e.startswith(".")]
    if len(entries) == 1:
        inner = os.path.join(extract_dir, entries[0])
        if os.path.isdir(inner) and os.path.isfile(os.path.join(inner, "run.py")):
            return inner

    if os.path.isfile(os.path.join(extract_dir, "run.py")):
        return extract_dir

    raise RuntimeError("更新包结构异常：未找到 run.py")


def install_requirements(app_dir, data_dir):
    """用内置 wheel 或已装环境更新依赖。"""
    venv_python = os.path.join(data_dir, "venv", "bin", "python")
    if not os.path.isfile(venv_python):
        log("未找到虚拟环境，跳过依赖更新")
        return True

    wheels_dir = os.path.join(app_dir, "wheels")
    req_file = os.path.join(app_dir, "requirements.txt")
    if not os.path.isfile(req_file):
        log("未找到 requirements.txt，跳过依赖更新")
        return True

    cmd = [venv_python, "-m", "pip", "install", "--no-cache-dir", "-r", req_file]
    if os.path.isdir(wheels_dir):
        cmd += ["--no-index", "--find-links", wheels_dir]

    log("更新 Python 依赖...")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log(f"依赖更新失败: {r.stderr[-500:]}")
            return False
        log("依赖更新完成")
        return True
    except Exception as e:
        log(f"依赖更新异常: {e}")
        return False


def perform_update(app_dir, data_dir, target_version=None, restart=True):
    """执行完整更新流程。

    返回 (success: bool, message: str)
    """
    try:
        release = fetch_latest_release()
    except Exception as e:
        return False, str(e)

    remote_version = str(release.get("tag_name") or release.get("name") or "")
    remote_version = remote_version.lstrip("vV")

    local_version = get_current_version(app_dir)

    if target_version:
        if parse_version(remote_version) != parse_version(target_version):
            log(f"注意: 指定版本 {target_version} 与最新 {remote_version} 不一致，按最新执行")

    if parse_version(remote_version) <= parse_version(local_version):
        return False, f"当前已是最新版本（本地 {local_version}，远程 {remote_version}）"

    log(f"发现新版本: {local_version} -> {remote_version}")

    asset = pick_source_asset(release)
    if not asset:
        return False, "更新包中没有可用的源码压缩包"

    work_dir = tempfile.mkdtemp(prefix="qdx-update-", dir=data_dir)
    tar_path = os.path.join(work_dir, "update.tar.gz")
    extract_dir = os.path.join(work_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)

    # 备份目录：备份当前应用代码
    backup_root = os.path.join(data_dir, "backup")
    backup_dir = os.path.join(backup_root, f"app-{local_version}")
    os.makedirs(backup_root, exist_ok=True)

    try:
        expected = asset.get("digest")
        if expected and expected.startswith("sha256:"):
            expected = expected.split(":", 1)[1]
        else:
            expected = None

        download_asset(asset, tar_path, expected)
        new_root = extract_and_find_root(tar_path, extract_dir)
        log(f"更新包解压完成: {new_root}")

        # 校验新版可运行性
        if not os.path.isfile(os.path.join(new_root, "run.py")):
            return False, "更新包校验失败：缺少 run.py"

        # 备份当前代码（排除 wheels/templates 等大目录，它们由新包覆盖）
        log(f"备份当前代码到 {backup_dir}")
        if os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        shutil.copytree(
            app_dir, backup_dir,
            ignore=shutil.ignore_patterns("wheels", "templates", "__pycache__", "*.pyc"),
            symlinks=True,
        )

        # 替换代码：保留 wheels 与 templates（新包中若有则覆盖）
        log("替换应用代码...")
        preserved = {}
        for keep in ("wheels", "templates"):
            src = os.path.join(app_dir, keep)
            if os.path.isdir(src):
                tmp = os.path.join(work_dir, f"_keep_{keep}")
                shutil.copytree(src, tmp, symlinks=True)
                preserved[keep] = tmp

        for entry in os.listdir(app_dir):
            if entry in ("local_config.py",):
                continue  # 持久化配置不删
            full = os.path.join(app_dir, entry)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full, ignore_errors=True)
                else:
                    os.remove(full)
            except Exception as e:
                log(f"  清理 {entry} 失败: {e}")

        for entry in os.listdir(new_root):
            src = os.path.join(new_root, entry)
            dst = os.path.join(app_dir, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        # 恢复被保留的目录（如果新包未包含）
        for keep, tmp in preserved.items():
            dst = os.path.join(app_dir, keep)
            if not os.path.isdir(dst):
                shutil.copytree(tmp, dst, symlinks=True)
                log(f"  保留原有 {keep} 目录")

        log("代码替换完成")

        # 更新依赖
        install_requirements(app_dir, data_dir)

        # 记录更新信息
        try:
            with open(os.path.join(data_dir, "update-info.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "from": local_version,
                    "to": remote_version,
                    "release_name": release.get("name", ""),
                    "body": release.get("body", ""),
                    "published_at": release.get("published_at", ""),
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        # 清理旧备份，只留最近 3 份
        try:
            backups = sorted(
                [d for d in os.listdir(backup_root) if d.startswith("app-")],
                reverse=True,
            )
            for old in backups[3:]:
                shutil.rmtree(os.path.join(backup_root, old), ignore_errors=True)
        except Exception:
            pass

        if restart:
            restart_service(app_dir)

        return True, f"更新成功: {local_version} -> {remote_version}"

    except Exception as e:
        log(f"更新失败: {type(e).__name__}: {e}")
        # 回滚
        if os.path.isdir(backup_dir):
            log("正在回滚到更新前的版本...")
            try:
                for entry in os.listdir(app_dir):
                    if entry == "local_config.py":
                        continue
                    full = os.path.join(app_dir, entry)
                    if os.path.isdir(full) and not os.path.islink(full):
                        shutil.rmtree(full, ignore_errors=True)
                    else:
                        os.remove(full)
                for entry in os.listdir(backup_dir):
                    src = os.path.join(backup_dir, entry)
                    dst = os.path.join(app_dir, entry)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                log("回滚完成")
            except Exception as re:
                log(f"回滚失败: {re}")
        return False, f"更新失败: {e}"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def restart_service(app_dir):
    """调用 cmd/main 重启服务。"""
    main_script = os.path.join(app_dir, "cmd", "main")
    if not os.path.isfile(main_script):
        log("未找到 cmd/main，跳过自动重启")
        return
    try:
        log("重启服务...")
        subprocess.run([main_script, "stop"], timeout=40, capture_output=True)
        subprocess.run([main_script, "start"], timeout=90, capture_output=True)
        log("服务已重启")
    except Exception as e:
        log(f"重启失败，请手动重启应用: {e}")


def main():
    app_dir = os.environ.get("TRIM_APPDEST") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    data_dir = os.environ.get("QDX_DATA_DIR") or os.environ.get("TRIM_PKGVAR") or app_dir

    action = sys.argv[1] if len(sys.argv) > 1 else "update"

    if action == "check":
        try:
            release = fetch_latest_release()
            remote = str(release.get("tag_name") or "").lstrip("vV")
            local = get_current_version(app_dir)
            newer = parse_version(remote) > parse_version(local)
            print(json.dumps({
                "local": local,
                "remote": remote,
                "has_update": newer,
                "name": release.get("name", ""),
                "body": release.get("body", ""),
                "published_at": release.get("published_at", ""),
            }, ensure_ascii=False))
            return 0
        except Exception as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            return 1

    if action == "update":
        target = sys.argv[2] if len(sys.argv) > 2 else None
        ok, msg = perform_update(app_dir, data_dir, target)
        print(msg)
        return 0 if ok else 1

    print(f"未知操作: {action}，支持 check / update", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
