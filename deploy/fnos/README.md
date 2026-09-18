# QDX 飞牛 fnOS 打包说明

QDX 提供完整的飞牛 fnOS 应用包（`.fpk`），支持离线安装、内置模板库、程序内自动更新。

## 快速开始

### 打包

```bash
# 完整构建：准备模板、下载依赖、同步源码、调用 fnpack
python build_fpk.py

# 依赖与模板已就绪时加速构建
python build_fpk.py --skip-deps --skip-templates

# 指定版本号
python build_fpk.py --version 1.0.1
```

产物：`deploy/fnos/qdx/qdx.fpk`

### 安装

飞牛「应用中心 → 手动安装」上传 `.fpk` 文件。安装向导会让你设置端口等参数。

安装完成后从应用中心点击图标打开。默认管理员账号 `admin`，密码 `admin`。

### 发布

打 tag 即自动构建并发布 Release：

```bash
git tag v1.0.1
git push origin v1.0.1
```

CI 会构建 FPK 与源码包，一起挂到 Release 上。

---

## 设计要点

### 为什么能离线安装

FPK 包内置了目标平台的全部 Python 依赖 wheel：

- 平台：`manylinux2014_x86_64`
- Python：`cp311`（对应飞牛内置的 Python 3.11）

安装时 `install_callback` 使用 `pip install --no-index --find-links ./wheels`，
强制不访问网络。依赖共 18MB。

**pycurl 已剔除**：PyPI 上没有 Linux wheel，需编译且依赖 libcurl 开发库。
QDX 对 pycurl 缺失会自动降级到 aiohttp（代码中是 `if pycurl:` 判断），功能不受影响。

### 数据持久化

飞牛升级时会替换 `target` 目录（即应用源码），因此**数据库与配置必须放在
`TRIM_PKGVAR`**。实现方式是利用 QD 官方的 `local_config.py` 覆盖机制：

```python
# 由 cmd/install_callback 自动生成
class sqlite3:
    path = os.path.join(_QDX_DATA, "database.db")
```

`local_config.py` 位于源码目录，升级时会被重建（`upgrade_callback` 负责），
而它指向的数据库始终在 `TRIM_PKGVAR`，所以数据不会丢。

### 目录映射

| 变量 | 用途 | QDX 的使用 |
|---|---|---|
| `TRIM_APPDEST` | 应用源码与静态资源 | 代码、wheels、templates |
| `TRIM_PKGVAR` | 重启后保留的运行数据 | 数据库、venv、日志、备份 |
| `TRIM_PKGETC` | 应用配置 | `env.sh` |
| `TRIM_PKGTMP` | 临时文件 | 更新解压 |

### 内置模板库

打包了上游模板库的 388 个公共模板快照。关键点：`tpls_history.json` 的
`content` 字段已内嵌 base64 编码的模板内容，因此导入过程**完全离线**。

导入的模板归属「内置模板」订阅源（`repourl = builtin://qdx`），在界面上
可单独识别与管理。

`import_templates.py` 是幂等的，支持三种模式：

```bash
import_templates.py                # 跳过已存在（默认）
import_templates.py --incremental  # 增量同步（升级时用）
import_templates.py --force        # 强制覆盖
```

### 程序内自动更新

对接 `https://github.com/sddvcm/qd` 的 Releases，**更新不需要重装 FPK**。

流程：

1. 请求 GitHub API 获取最新 Release
2. 与本地 `version.json` 比较版本号
3. 下载 Release 附件中的源码包
4. 备份当前代码到 `TRIM_PKGVAR/backup/app-<version>`
5. 解压并替换代码（保留 `local_config.py` 与数据库）
6. 增量更新依赖
7. 重启服务

任何一步失败都会自动回滚到备份。

**两个实现细节值得注意**：

- **显式绕过环境代理**。NAS 上常见的 Clash 等代理会对 GitHub API 返回 403，
  导致更新检查误报为网络故障。`http_get` 用 `ProxyHandler({})` 构造 opener 直连。
- **区分 404 与网络故障**。`/releases/latest` 在仓库没有 Release 时返回 404。
  这属于正常状态，需要给出「请先创建 Release」这类可执行的提示，
  而不是笼统的「无法连接服务器」。

界面入口：导航栏「系统更新」（仅管理员可见）。

---

## 常见问题

### 更新检查提示「更新源尚无发布版本」

仓库还没有创建过 Release。访问
`https://github.com/sddvcm/qd/releases` 创建一个，CI 会自动填充内容。

### 安装时提示找不到 Python 3.8+

飞牛的 Python 版本低于要求。确认系统版本不低于 manifest 中声明的
`os_min_version`。

### 依赖安装失败

安装日志在 `TRIM_PKGVAR/install.log`。常见原因：

- wheel 与目标 Python 版本不匹配（本机开发用的 Python 与飞牛不同）
- 存储空间不足（需要约 200MB 用于 venv）

### 升级后模板没有更新

`upgrade_callback` 中的模板同步失败不会中断升级（设计如此）。
可手动执行：

```bash
cd $TRIM_APPDEST
$TRIM_PKGVAR/venv/bin/python deploy/fnos/scripts/import_templates.py --incremental
```

---

## 关键文件索引

| 文件 | 作用 |
|---|---|
| `build_fpk.py` | 构建入口 |
| `deploy/fnos/qdx/manifest` | 应用元数据（飞牛 INI 格式） |
| `deploy/fnos/qdx/cmd/main` | 启动/停止/状态 |
| `deploy/fnos/qdx/cmd/install_callback` | 离线装依赖 + 导入模板 |
| `deploy/fnos/qdx/cmd/upgrade_init` | 升级前备份 |
| `deploy/fnos/qdx/cmd/upgrade_callback` | 升级后重建配置 |
| `deploy/fnos/scripts/import_templates.py` | 模板导入 |
| `deploy/fnos/scripts/updater.py` | 自动更新 |
| `web/handlers/system.py` | 更新相关接口 |
| `web/handlers/repo_io.py` | 订阅源导入导出 |
| `.github/workflows/build-fpk-release.yml` | CI 构建与发布 |
| `.gitattributes` | 换行符规则（保证脚本为 LF） |
