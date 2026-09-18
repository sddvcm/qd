#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QDX 内置模板导入脚本

把安装包内 templates/tpls_history.json 中的模板导入 QDX 数据库。

设计要点:
  - tpls_history.json 的 content 字段已内嵌 base64 模板内容，因此导入过程
    完全离线，不需要请求任何网络资源。
  - 幂等：按 (name, reponame) 判断是否已存在。默认跳过已存在项，
    加 --incremental 时只导入新增，加 --force 时覆盖更新。
  - 导入的模板归属一个名为 "内置模板" 的订阅源，reponame 稳定，
    便于用户在界面上单独管理。

用法:
    import_templates.py              # 导入全部内置模板（跳过已存在）
    import_templates.py --incremental # 增量同步（升级时使用）
    import_templates.py --force      # 强制覆盖
"""

import asyncio
import json
import os
import sys

# 内置模板源的标识，保持稳定，不要随意修改
BUILTIN_REPO_NAME = "内置模板"
BUILTIN_REPO_URL = "builtin://qdx"
BUILTIN_REPO_BRANCH = "local"

DEFAULT_HISTORY = os.path.join("templates", "tpls_history.json")


def find_history_file():
    """定位 tpls_history.json，优先使用应用目录内的内置快照。"""
    app_dir = os.environ.get("TRIM_APPDEST") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    candidate = os.path.join(app_dir, DEFAULT_HISTORY)
    if os.path.isfile(candidate):
        return candidate

    # 退化：相对当前工作目录查找
    if os.path.isfile(DEFAULT_HISTORY):
        return os.path.abspath(DEFAULT_HISTORY)

    return None


async def import_templates(mode="skip"):
    """执行导入。mode: skip / incremental / force"""
    # 延迟导入，确保在正确的工作目录与配置下加载
    from db import DB
    from db import db_converter

    history_path = find_history_file()
    if not history_path:
        print(f"[ERROR] 未找到模板快照文件 tpls_history.json", file=sys.stderr)
        return 1

    print(f"[INFO] 模板快照: {history_path}")

    with open(history_path, "r", encoding="utf-8") as f:
        history = json.load(f)

    har_map = history.get("har", {})
    if not har_map:
        print("[ERROR] 模板快照为空", file=sys.stderr)
        return 1

    print(f"[INFO] 快照包含 {len(har_map)} 个模板")

    db = DB()

    # 建表与结构迁移由 db_converter 负责，与 run.py 启动流程保持一致。
    # 这样即便导入脚本先于服务首次启动运行，数据库结构也是完整的。
    try:
        converter = db_converter.DBconverter(db)
        await converter.convert_new_type(db)
        print("[INFO] 数据库结构检查完成")
    except Exception as e:
        print(f"[WARN] 数据库结构检查异常，继续尝试导入: {e}", file=sys.stderr)

    added = 0
    updated = 0
    skipped = 0
    failed = 0

    for key, har in har_map.items():
        try:
            name = har.get("name") or key
            content = har.get("content", "")

            # content 为空说明快照不完整，跳过（不请求网络）
            if not content:
                failed += 1
                continue

            # 构造写入 pubtpl 表的字段
            record = {
                "name": name,
                "author": har.get("author", ""),
                "comments": har.get("comments", ""),
                "content": content,
                "filename": har.get("filename", ""),
                "date": har.get("date", ""),
                "version": str(har.get("version", 1)),
                "url": har.get("url", ""),
                "update": "True",
                "reponame": BUILTIN_REPO_NAME,
                "repourl": BUILTIN_REPO_URL,
                "repoacc": "False",
                "repobranch": BUILTIN_REPO_BRANCH,
                "commenturl": har.get("commenturl", ""),
            }

            existing = await db.pubtpl.list(
                name=name,
                reponame=BUILTIN_REPO_NAME,
                fields=("id", "version"),
            )

            if existing:
                if mode == "force":
                    await db.pubtpl.mod(existing[0]["id"], **record)
                    updated += 1
                else:
                    skipped += 1
                continue

            await db.pubtpl.add(record)
            added += 1

            if (added + updated) % 50 == 0:
                print(f"[INFO] 已处理 {added + updated} 个模板...")

        except Exception as e:
            failed += 1
            print(f"[WARN] 模板 {har.get('name', key)} 导入失败: {e}", file=sys.stderr)

    print()
    print("=" * 50)
    print(f"新增: {added}")
    print(f"更新: {updated}")
    print(f"跳过(已存在): {skipped}")
    print(f"失败: {failed}")
    print("=" * 50)

    return 0


def main():
    mode = "skip"
    if "--force" in sys.argv:
        mode = "force"
    elif "--incremental" in sys.argv:
        mode = "skip"

    try:
        return asyncio.run(import_templates(mode))
    except KeyboardInterrupt:
        print("\n[INFO] 用户中断")
        return 130
    except Exception as e:
        print(f"[ERROR] 导入过程出错: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
