<p align="center">
   <a href="https://github.com/sddvcm/qd">
   <img style="border-radius:50%" width="150" src="web/static/img/icon.png">
   </a>
</p>

<h1 align="center">QDX</h1>

<div align="center">

QDX —— 一个<b>HTTP 请求定时任务自动执行框架</b> base on HAR Editor and Tornado Server

[![Github][Github-image]][Github-url]
[![license][github-license-image]][github-license-url]
![python version][python-version-image]

</div>

---

## 这是什么

QDX 是对 [QD](https://github.com/qd-today/qd)（MIT 协议）的二次开发版本，保留了 QD 全部核心能力，并做了以下改动：

- **完全脱离上游关联**：代码、部署、更新、插件订阅全链路不再依赖 `qd-today/qd` 及其关联仓库。
- **镜像与 CI 自有化**：构建产物推送到本项目自己的命名空间，CI 仅在本仓库触发。
- **模板源可控**：默认不预置任何外部模板仓库，由使用者自行配置。
- **品牌独立**：界面、文档、元数据统一为 QDX。

核心功能与 QD 一致：

- 通过 HAR Editor 录制浏览器请求，一键生成定时任务
- 支持变量、模板继承、公共模板订阅
- 多种推送方式（WxPusher / 钉钉 / 企业微信 / Server酱 / Bark / Telegram / 邮件等）
- 多用户、权限管理、任务日志、失败重试

## 部署

### Docker Compose（推荐）

```yaml
services:
  qdx:
    image: ghcr.io/sddvcm/qd:latest
    container_name: qdx
    restart: always
    ports:
      - "8080:80"
    volumes:
      - ./config:/usr/src/app/config
```

```bash
docker compose up -d
```

浏览器打开 `http://<你的IP>:8080`，默认管理员账号 `admin`，密码 `admin`。

### 本地运行

```bash
pip install -r requirements.txt
python run.py
```

## 模板仓库配置

QDX **默认不订阅任何外部模板仓库**。首次部署后公共模板列表为空，这是有意设计——避免隐式依赖外部源。

如需启用公共模板，登录管理员账号后进入 `公共模板` → `管理订阅仓库`，添加你自己的模板仓库地址。模板仓库格式与 QD 模板库一致（一个含 `tpls/` 目录的 git 仓库）。

## 更新

```bash
docker compose pull && docker compose up -d
```

容器内的 `update` 命令仅使用本仓库自身的 `requirements.txt`，不会从任何外部源拉取。

## 与上游的关系

QDX 基于 QD (MIT License, Copyright (c) 2021 QD-Today) 二次开发。

根据 MIT 协议要求，原始版权声明与许可文本完整保留在 [LICENSE](LICENSE) 中。上游作者与贡献者名单见 [.all-contributorsrc](.all-contributorsrc) 与 [CHANGELOG.md](CHANGELOG.md)。

除上述许可合规所需的署名外，QDX 的运行、部署、更新、CI、镜像、模板订阅均已与上游脱离关联。上游项目的任何变更不会影响 QDX。

## 许可

[MIT](LICENSE)

## 链接

[Github-image]: https://img.shields.io/static/v1?label=Github&message=QDX&color=brightgreen
[Github-url]: https://github.com/sddvcm/qd/
[github-license-image]: https://img.shields.io/github/license/sddvcm/qd
[github-license-url]: https://github.com/sddvcm/qd/blob/master/LICENSE
[python-version-image]: https://img.shields.io/badge/python-3.6+-blue.svg
