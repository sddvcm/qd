#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# QDX 扩展：程序内自动更新
#
# 对接 https://github.com/sddvcm/qd 的 Releases，实现不重装 FPK 的原地更新。
#
# 接口:
#   GET  /system/version          当前版本与更新源信息
#   GET  /system/check_update     检查是否有新版本
#   POST /system/do_update        执行更新
#   GET  /system/update_log       查看最近一次更新的结果

import json
import os
import subprocess
import sys
import time

from tornado.web import authenticated

import config
from web.handlers.base import BaseHandler, logger_web_handler

# 更新器脚本路径（相对应用根目录）
UPDATER_REL = os.path.join("deploy", "fnos", "scripts", "updater.py")

# 更新源
UPDATE_REPO = "sddvcm/qd"
UPDATE_REPO_URL = f"https://github.com/{UPDATE_REPO}"


def _app_dir():
    """定位应用根目录。"""
    # web/handlers/system.py -> 上溯三层即应用根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _python_bin():
    """优先使用 FPK 安装的虚拟环境 Python，退化到系统 Python。"""
    data_dir = os.environ.get("QDX_DATA_DIR") or os.environ.get("TRIM_PKGVAR") or ""
    if data_dir:
        candidate = os.path.join(data_dir, "venv", "bin", "python")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return sys.executable or "python3"


def _read_local_version():
    p = os.path.join(_app_dir(), "version.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return str(json.load(f).get("version", "0"))
    except Exception:
        return "0"


class VersionHandler(BaseHandler):
    """返回当前版本与更新源信息。"""

    @authenticated
    async def get(self, userid):
        data_dir = os.environ.get("QDX_DATA_DIR") or os.environ.get("TRIM_PKGVAR") or ""
        info = {
            "code": 1,
            "version": _read_local_version(),
            "update_repo": UPDATE_REPO,
            "update_repo_url": UPDATE_REPO_URL,
            "releases_url": f"{UPDATE_REPO_URL}/releases",
        }

        # 附带最近一次更新的记录
        if data_dir:
            p = os.path.join(data_dir, "update-info.json")
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        info["last_update"] = json.load(f)
                except Exception:
                    pass

        self.finish(info)


class CheckUpdateHandler(BaseHandler):
    """检查是否有新版本。调用 updater.py 的 check 动作。"""

    @authenticated
    async def get(self, userid):
        user = self.current_user
        if user["role"] != "admin":
            self.set_status(403)
            self.finish({"code": 0, "message": "需要管理员权限"})
            return

        script = os.path.join(_app_dir(), UPDATER_REL)
        if not os.path.isfile(script):
            self.finish({
                "code": 0,
                "message": "更新模块不存在。此功能仅在使用飞牛 FPK 安装包时可用。",
            })
            return

        try:
            env = os.environ.copy()
            env.setdefault("QDX_DATA_DIR", os.environ.get("TRIM_PKGVAR", ""))
            result = subprocess.run(
                [_python_bin(), script, "check"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
                cwd=_app_dir(),
            )
            out = (result.stdout or "").strip()
            # 取最后一行作为 JSON 结果（前面可能有日志）
            last_line = out.splitlines()[-1] if out else "{}"
            payload = json.loads(last_line)

            if "error" in payload:
                self.finish({
                    "code": 0,
                    "message": f"检查更新失败: {payload['error']}",
                })
                return

            payload["code"] = 1
            payload["local"] = payload.get("local", _read_local_version())
            self.finish(payload)

        except subprocess.TimeoutExpired:
            self.finish({"code": 0, "message": "检查更新超时，请检查网络连接"})
        except Exception as e:
            logger_web_handler.error("检查更新异常: %s", e)
            self.finish({"code": 0, "message": f"检查更新失败: {e}"})


class DoUpdateHandler(BaseHandler):
    """执行更新。整个流程由 updater.py 负责，含备份与回滚。"""

    @authenticated
    async def post(self, userid):
        user = self.current_user
        if user["role"] != "admin":
            self.set_status(403)
            self.finish({"code": 0, "message": "需要管理员权限"})
            return

        script = os.path.join(_app_dir(), UPDATER_REL)
        if not os.path.isfile(script):
            self.finish({"code": 0, "message": "更新模块不存在。"})
            return

        target = None
        try:
            if self.request.body:
                body = json.loads(self.request.body.decode("utf-8"))
                target = body.get("version")
        except Exception:
            pass

        data_dir = os.environ.get("QDX_DATA_DIR") or os.environ.get("TRIM_PKGVAR") or ""

        try:
            env = os.environ.copy()
            env.setdefault("QDX_DATA_DIR", data_dir)
            cmd = [_python_bin(), script, "update"]
            if target:
                cmd.append(str(target))

            logger_web_handler.info("UserID: %s 触发程序内更新", userid)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                env=env,
                cwd=_app_dir(),
            )
            out = (result.stdout or "").strip()
            lines = out.splitlines()
            message = lines[-1] if lines else "更新完成"

            if data_dir:
                log_path = os.path.join(data_dir, "update-run.log")
                try:
                    with open(log_path, "w", encoding="utf-8") as f:
                        f.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        f.write(out)
                except Exception:
                    pass

            if result.returncode == 0:
                self.finish({
                    "code": 1,
                    "message": message,
                    "detail": lines[-30:],
                })
            else:
                self.finish({
                    "code": 0,
                    "message": message,
                    "detail": lines[-30:],
                })

        except subprocess.TimeoutExpired:
            self.finish({"code": 0, "message": "更新超时，请查看服务日志确认状态"})
        except Exception as e:
            logger_web_handler.error("执行更新异常: %s", e)
            self.finish({"code": 0, "message": f"更新失败: {e}"})


class UpdateLogHandler(BaseHandler):
    """查看最近一次更新的完整日志。"""

    @authenticated
    async def get(self, userid):
        user = self.current_user
        if user["role"] != "admin":
            self.set_status(403)
            self.finish({"code": 0, "message": "需要管理员权限"})
            return

        data_dir = os.environ.get("QDX_DATA_DIR") or os.environ.get("TRIM_PKGVAR") or ""
        if not data_dir:
            self.finish({"code": 0, "message": "无法定位数据目录"})
            return

        log_path = os.path.join(data_dir, "update-run.log")
        if not os.path.isfile(log_path):
            self.finish({"code": 1, "log": "", "message": "暂无更新日志"})
            return

        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            self.finish({"code": 1, "log": content[-20000:]})
        except Exception as e:
            self.finish({"code": 0, "message": f"读取日志失败: {e}"})


class UpdatePageHandler(BaseHandler):
    """系统更新页面。"""

    @authenticated
    async def get(self, userid):
        user = self.current_user
        if user["role"] != "admin":
            self.set_status(403)
            self.finish("需要管理员权限")
            return
        await self.render(
            "system_update.html",
            update_repo=UPDATE_REPO,
            update_repo_url=UPDATE_REPO_URL,
        )


handlers = [
    (r"/system/update/?", UpdatePageHandler),
    (r"/system/version/?", VersionHandler),
    (r"/system/check_update/?", CheckUpdateHandler),
    (r"/system/do_update/?", DoUpdateHandler),
    (r"/system/update_log/?", UpdateLogHandler),
]
