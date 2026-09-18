#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# QDX 扩展：订阅源与模板的导入导出
#
# 提供三个能力:
#   1. 导出订阅源配置  - 管理员一键导出当前所有订阅源为 JSON 文件
#   2. 导入订阅源配置  - 从 JSON 文件恢复订阅源
#   3. 导出模板库     - 把已导入的公共模板导出为可迁移的 JSON 文件
#
# 设计说明:
#   QDX 的订阅源存储在 site 表的 repos 字段（JSON 字符串）。
#   导出时直接序列化该结构，导入时合并去重，因此导入导出可跨实例迁移。

import json
import time
import urllib.parse

from tornado.web import authenticated

import config
from web.handlers.base import BaseHandler, logger_web_handler

# 导出文件的格式版本，便于后续兼容处理
EXPORT_FORMAT_VERSION = 1


class RepoExportHandler(BaseHandler):
    """导出当前订阅源配置为 JSON 文件。"""

    @authenticated
    async def get(self, userid):
        user = self.current_user
        if user["role"] != "admin":
            self.set_status(403)
            self.finish({"code": 0, "message": "需要管理员权限"})
            return

        try:
            site = await self.db.site.get(1, fields=("repos",))
            repos_data = json.loads(site["repos"]) if site and site.get("repos") else {"repos": [], "lastupdate": 0}
        except Exception as e:
            logger_web_handler.error("导出订阅源失败: %s", e)
            self.set_status(500)
            self.finish({"code": 0, "message": f"读取订阅源失败: {e}"})
            return

        payload = {
            "format": "qdx-repos",
            "format_version": EXPORT_FORMAT_VERSION,
            "exported_at": int(time.time()),
            "version": getattr(config, "version", ""),
            "repos": repos_data.get("repos", []),
        }

        content = json.dumps(payload, ensure_ascii=False, indent=2)
        filename = f"qdx-repos-{time.strftime('%Y%m%d%H%M%S')}.json"

        self.set_header("Content-Type", "application/json; charset=utf-8")
        # 用 filename* 确保中文文件名正确编码
        self.set_header(
            "Content-Disposition",
            "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
                "qdx-repos.json", urllib.parse.quote(filename)
            ),
        )
        self.finish(content)


class RepoImportHandler(BaseHandler):
    """从上传的 JSON 文件导入订阅源配置。"""

    @authenticated
    async def post(self, userid):
        user = self.current_user
        if user["role"] != "admin":
            self.set_status(403)
            self.finish({"code": 0, "message": "需要管理员权限"})
            return

        # 支持两种输入: JSON body 或表单字段 content
        raw = None
        if self.request.files:
            file_info = self.request.files.get("file")
            if file_info and len(file_info) > 0:
                raw = file_info[0]["body"].decode("utf-8", "ignore")
        if raw is None:
            body = self.request.body
            if body:
                raw = body.decode("utf-8", "ignore")

        if not raw:
            self.finish({"code": 0, "message": "未收到导入内容"})
            return

        try:
            payload = json.loads(raw)
        except Exception as e:
            self.finish({"code": 0, "message": f"JSON 解析失败: {e}"})
            return

        # 兼容两种格式: 完整导出文件 / 裸 repos 数组
        if isinstance(payload, dict):
            incoming = payload.get("repos", [])
        elif isinstance(payload, list):
            incoming = payload
        else:
            self.finish({"code": 0, "message": "文件格式不正确，期望 JSON 对象或数组"})
            return

        if not isinstance(incoming, list):
            self.finish({"code": 0, "message": "repos 字段必须是数组"})
            return

        try:
            site = await self.db.site.get(1, fields=("repos",))
            current = json.loads(site["repos"]) if site and site.get("repos") else {"repos": [], "lastupdate": 0}
        except Exception:
            current = {"repos": [], "lastupdate": 0}

        existing = current.get("repos", []) or []
        # 按 repourl 去重
        seen = {r.get("repourl", "") for r in existing if isinstance(r, dict)}

        added = 0
        skipped = 0
        for item in incoming:
            if not isinstance(item, dict):
                skipped += 1
                continue
            url = item.get("repourl", "")
            if not url or url in seen:
                skipped += 1
                continue
            # 补齐缺失字段，避免界面渲染出错
            existing.append({
                "reponame": item.get("reponame") or url.rstrip("/").split("/")[-1] or "导入仓库",
                "repourl": url,
                "repobranch": item.get("repobranch") or "master",
                "repoacc": bool(item.get("repoacc", False)),
            })
            seen.add(url)
            added += 1

        current["repos"] = existing
        # 置零以触发立即更新
        current["lastupdate"] = 0

        try:
            await self.db.site.mod(1, repos=json.dumps(current, ensure_ascii=False, indent=4))
        except Exception as e:
            logger_web_handler.error("导入订阅源写入失败: %s", e)
            self.finish({"code": 0, "message": f"写入数据库失败: {e}"})
            return

        logger_web_handler.info(
            "UserID: %s 导入订阅源 %s 个，跳过 %s 个", userid, added, skipped
        )
        self.finish({
            "code": 1,
            "message": f"导入完成：新增 {added} 个订阅源，跳过 {skipped} 个",
            "added": added,
            "skipped": skipped,
        })


class TemplateExportHandler(BaseHandler):
    """导出已导入的公共模板库，便于迁移到其他实例。"""

    @authenticated
    async def get(self, userid):
        user = self.current_user
        if user["role"] != "admin":
            self.set_status(403)
            self.finish({"code": 0, "message": "需要管理员权限"})
            return

        try:
            # 分批取全部模板
            tpls = []
            limit = 500
            offset = 0
            while True:
                batch = await self.db.pubtpl.list(
                    fields=("name", "author", "comments", "content", "filename",
                            "version", "url", "reponame", "repourl", "repobranch"),
                    limit=limit,
                )
                if not batch:
                    break
                tpls.extend(batch)
                if len(batch) < limit:
                    break
                offset += limit
                if offset > 20000:  # 安全上限
                    break
        except Exception as e:
            logger_web_handler.error("导出模板库失败: %s", e)
            self.set_status(500)
            self.finish({"code": 0, "message": f"读取模板失败: {e}"})
            return

        # 按 QD 的 tpls_history.json 格式组织，可直接作为订阅源使用
        har_map = {}
        for t in tpls:
            key = t.get("name") or t.get("filename") or ""
            if not key:
                continue
            har_map[key] = {
                "name": t.get("name", ""),
                "author": t.get("author", ""),
                "url": t.get("url", ""),
                "update": True,
                "comments": t.get("comments", ""),
                "filename": t.get("filename", ""),
                "content": t.get("content", ""),
                "version": t.get("version", "1"),
            }

        payload = {
            "format": "qdx-templates",
            "format_version": EXPORT_FORMAT_VERSION,
            "version": EXPORT_FORMAT_VERSION,
            "exported_at": int(time.time()),
            "count": len(har_map),
            "har": har_map,
        }

        content = json.dumps(payload, ensure_ascii=False)
        filename = f"qdx-templates-{time.strftime('%Y%m%d%H%M%S')}.json"

        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.set_header(
            "Content-Disposition",
            "attachment; filename=\"qdx-templates.json\"; filename*=UTF-8''{}".format(
                urllib.parse.quote(filename)
            ),
        )
        self.finish(content)


handlers = [
    (r"/subscribe/repos/export/?", RepoExportHandler),
    (r"/subscribe/repos/import/?", RepoImportHandler),
    (r"/subscribe/templates/export/?", TemplateExportHandler),
]
