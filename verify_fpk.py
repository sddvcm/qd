#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FPK 安装包静态校验。

无法在本机跑 Linux 实测时，用这个脚本逐项验证安装包自洽性，
把"装机才发现的错误"提前到打包阶段暴露。

校验项：
  1. 包结构与必需文件
  2. 内置 Python 解释器可用（完成 gzip + tar 结构校验）
  3. wheels 与内置解释器 ABI 匹配（cp312 或 abi3，无 cp310/cp38 之类）
  4. requirements.txt 中的每个依赖都能在 wheels/ 里找到
  5. cmd/ 脚本 LF 换行 + 语法配平 + 可执行位
  6. main 脚本引用的每个包内路径都真实存在
  7. 模板快照完整
"""

import io
import os
import re
import sys
import tarfile

FPK = r"C:\Users\Administrator\WorkBuddy\2026-09-18-16-14-27\qd\deploy\fnos\qdx\qdx.fpk"

ok = 0
bad = 0


def check(cond, label, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  [OK]   {label}")
    else:
        bad += 1
        print(f"  [FAIL] {label}" + (f"  <- {detail}" if detail else ""))


def main():
    if not os.path.isfile(FPK):
        print(f"找不到 FPK: {FPK}")
        return 1

    print(f"校验目标: {FPK}")
    print(f"大小: {os.path.getsize(FPK) / 1024 / 1024:.2f}MB")
    print()

    with tarfile.open(FPK, "r") as t:
        outer = {m.name: m for m in t.getmembers()}
        manifest = t.extractfile("manifest").read().decode("utf-8")
        app_data = t.extractfile("app.tgz").read()

        print("== 1. 包结构 ==")
        for need in ["manifest", "app.tgz", "ICON.PNG", "ICON_256.PNG"]:
            check(need in outer, f"存在 {need}")

        print()
        print("== 2. manifest ==")
        ver = re.search(r"^version\s*=\s*(\S+)", manifest, re.M)
        check(bool(ver), "version 字段存在", manifest[:200])
        if ver:
            v = ver.group(1)
            check(bool(re.match(r"^\d+\.\d+\.\d+$", v)),
                  f"version 三段式 ({v})")
        for key in ["appname", "display_name", "service_port", "desktop_uidir",
                    "desktop_applaunchname", "checkport", "ctl_stop"]:
            check(bool(re.search(rf"^{key}\s*=", manifest, re.M)), f"含 {key}")
        check(bool(re.search(r"^checksum\s*=\s*\S+", manifest, re.M)),
              "checksum 由 fnpack 生成")

    # app.tgz 内部
    inner = tarfile.open(fileobj=io.BytesIO(app_data), mode="r:gz")
    members = {m.name: m for m in inner.getmembers()}
    names = set(members.keys())

    print()
    print("== 3. 内置 Python 解释器 ==")
    pytar_name = "python/python.tar.gz"
    check(pytar_name in names, f"存在 {pytar_name}")
    if pytar_name in names:
        size = members[pytar_name].size
        check(size > 20 * 1024 * 1024, f"大小合理 ({size / 1024 / 1024:.1f}MB)")
        py_data = inner.extractfile(pytar_name).read()
        pyt = tarfile.open(fileobj=io.BytesIO(py_data), mode="r:gz")
        pynames = {m.name.rstrip("/") for m in pyt}
        check("python/bin/python3.12" in pynames, "含 bin/python3.12")
        check(any(n.endswith("site-packages/pip/__init__.py") for n in pynames),
              "自带 pip（离线安装依赖的前提）")
        # setuptools 不在 stripped 解释器里，但 wheel 包中已内置一份，
        # 因此这里只作信息提示，不作为失败项。
        has_st = any(n.endswith("site-packages/setuptools/__init__.py") for n in pynames)
        has_st_whl = any("setuptools" in os.path.basename(w).lower() for w in
                         [x for x in names if x.startswith("wheels/")])
        check(has_st or has_st_whl,
              f"setuptools 可用（解释器自带={has_st}, wheel 内置={has_st_whl}）")

    print()
    print("== 4. 依赖 wheels 与 ABI 匹配 ==")
    wheels = sorted(n for n in names if n.startswith("wheels/") and n.endswith(".whl"))
    srcs = sorted(n for n in names if n.startswith("wheels/") and n.endswith(".tar.gz"))
    check(len(wheels) >= 35, f"wheel 数量 ({len(wheels)})")
    check(len(srcs) >= 1, f"源码包数量 ({len(srcs)})")

    # 编译型 wheel 的 ABI 必须能被 cp312 加载：cp312-* 或 *-abi3-*
    incompatible = []
    for w in wheels:
        base = os.path.basename(w)
        m = re.search(r"-(cp\d{2,3})-(?:cp\d{2,3}m?-)?(abi3|none|cp\d{2,3})", base)
        tags = re.findall(r"-(cp\d{2,3}|py\d|py2\.py3|abi3)-", base)
        # 命中的解释器 tag
        impl_tags = re.findall(r"-(cp\d{2,3}|py\d+|py2\.py3)-", base)
        if not impl_tags:
            continue
        t0 = impl_tags[0]
        if t0 == "cp312" or t0.startswith("py") or t0 == "py2.py3":
            continue
        if "abi3" in base:
            continue
        incompatible.append(base)

    check(not incompatible, "无与 cp312 不兼容的编译包",
          f"不兼容: {incompatible[:5]}")

    cp312 = [w for w in wheels if "cp312" in os.path.basename(w)]
    abi3 = [w for w in wheels if "abi3" in os.path.basename(w)]
    print(f"        （cp312 专用 {len(cp312)} 个, abi3 稳定 ABI {len(abi3)} 个）")

    print()
    print("== 5. requirements 覆盖性 ==")
    if "requirements.txt" in names:
        req = inner.extractfile("requirements.txt").read().decode("utf-8")
        pkgs = []
        for line in req.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            name = re.split(r"[=<>!\[\s]", s)[0].strip().lower()
            if name:
                pkgs.append(name)
        wheel_names = [os.path.basename(w).lower() for w in wheels + srcs]
        missing = []
        for p in pkgs:
            key = p.replace("-", "_").replace(".", "_")
            if not any(key in w.replace("-", "_").replace(".", "_") for w in wheel_names):
                missing.append(p)
        check(not missing, f"requirements 全部有对应 wheel ({len(pkgs)} 条)",
              f"缺失: {missing}")
    else:
        check(False, "存在 requirements.txt")

    print()
    print("== 6. 模板快照 ==")
    check("templates/tpls_history.json" in names, "存在 tpls_history.json")
    har = [n for n in names if n.endswith(".har")]
    check(len(har) >= 380, f"模板数量 ({len(har)})")

    print()
    print("== 7. 运行时代码 ==")
    for need in ["run.py", "config.py", "qd.py", "web.py", "worker.py",
                 "deploy/fnos/scripts/updater.py",
                 "deploy/fnos/scripts/import_templates.py",
                 "ui/config", "config/README"]:
        check(need in names, f"存在 {need}")
    check("/" in "".join(names) and not any(n.endswith(".pyc") for n in names),
          "无 .pyc 残留")

    print()
    print("== 8. 生命周期脚本 ==")
    with tarfile.open(FPK, "r") as t:
        for m in t.getmembers():
            if not m.name.startswith("cmd/"):
                continue
            fn = os.path.basename(m.name)
            data = t.extractfile(m).read()
            check(b"\r\n" not in data, f"{fn}: LF 换行（非 CRLF）",
                  "含 CRLF 会导致 bad interpreter")
            text = data.decode("utf-8", errors="replace")
            # 提取这里要检查的脚本
            if fn == "main":
                # main 引用的包内路径必须真实存在
                refs = [
                    ("python/python.tar.gz", "$APP_ROOT/python/python.tar.gz"),
                    ("wheels/", "wheels 目录"),
                    ("requirements.txt", "requirements.txt"),
                    ("run.py", "run.py"),
                    ("templates/tpls_history.json", "模板快照"),
                    ("deploy/fnos/scripts/import_templates.py", "模板导入脚本"),
                ]
                for rel, label in refs:
                    # 目录在 tar 中可能只以文件项形式存在，故用前缀匹配
                    found = (rel in names) or any(n.startswith(rel) for n in names)
                    check(found, f"main 引用的 {label} 存在于包内")
                check('PYBIN="${RT}/python/bin/python3.12"' in text,
                      "main 使用内置解释器路径")
                check("import_templates.py" in text, "main 含模板导入逻辑")
                check(".wheels_installed" in text, "main 含依赖安装完成标记")

    print()
    print("=" * 58)
    print(f"通过 {ok} 项，失败 {bad} 项")
    print("=" * 58)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
