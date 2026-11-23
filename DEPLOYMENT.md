# 独享开源版：部署、更新与维护

适用全新 Debian 12／13，至少 8 GB 内存。此版无需项目激活码；仍需自行准备服务器和可使用 Claude Code 的账号。已有安装请看「更新」，不要重新初始化密码。

## Windows 一键部署

完整解压 `claude-cloud-opensource-windows.zip`，双击 `开始部署.exe`。填写服务器、SSH 登录信息和网站密码，首次核对 SSH 指纹，等待程序给出网址。保留同目录的 `payload`；安装时本地电脑需联网。程序不会替你设置云防火墙、DNS、Claude 登录或官方付款。

安装器使用 `/opt/claude-cloud-solo`。以下手动安装使用 `/opt/claude-cloud-opensource`。不要在同一台服务器上同时部署两份并争用 80／443。

## 全新 Debian 12／13 安装

先在云控制台放行实际 SSH 端口及 TCP 80、443。若使用域名，先把域名解析到这台服务器。源码包需要从你下载的本项目发布附件取得，下面不假设一个尚未发布的 GitHub 地址。

### 1. 本地上传源码 ZIP

在 Windows PowerShell 依次运行。第一行填写真实服务器地址，第二行填写 SSH 用户名；源码 ZIP 放在下载目录。

```powershell
$ClaudeServer = Read-Host '服务器公网 IP'
$ClaudeSshUser = Read-Host 'SSH 用户名，例如 root 或 debian'
$ClaudeSshPort = Read-Host 'SSH 端口，例如 22'
scp -P $ClaudeSshPort "$env:USERPROFILE\Downloads\claude-cloud-opensource-source.zip" "${ClaudeSshUser}@${ClaudeServer}:/tmp/claude-cloud-opensource-source.zip"
ssh -p $ClaudeSshPort "${ClaudeSshUser}@${ClaudeServer}"
```

### 2. 服务器安装 Docker

以下命令都在服务器终端运行。当前已是 `root` 就跳过第一条；普通 `debian` 用户先进入 root。这里只适用于全新服务器，已有 Docker 不需要重装。

```bash
sudo -i
apt-get update
apt-get install -y ca-certificates curl python3 unzip
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $VERSION_CODENAME
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker version
docker compose version
```

安装源和 Debian 支持范围参考 [Docker 官方文档](https://docs.docker.com/engine/install/debian/)。本项目要求 Docker Engine 28+；不要通过删除 apt 锁文件处理权限错误。

### 3. 解压并初始化独享版

```bash
test ! -e /opt/claude-cloud-opensource || { echo "目录已存在，请按更新流程处理"; exit 1; }
unzip -q /tmp/claude-cloud-opensource-source.zip -d /opt
cd /opt/claude-cloud-opensource
read -rp "网站公网 IP 或域名（不带 https://）: " CLAUDE_ADDRESS
python3 scripts/configure_solo.py --address "$CLAUDE_ADDRESS" --profile auto
python3 scripts/deploy_public.py "$CLAUDE_ADDRESS"
docker compose ps
```

配置脚本会要求输入并确认网站密码，至少 12 个字符；输入时不回显。它创建权限为 0600 的 `.env`，设置 `APP_MODE=solo`、HTTPS 和资源档位。没有命令行明文密码参数。

看到 `PASS trusted HTTPS and application health` 后，打开输出的网址，用网站密码登录。在菜单里连接 Claude，完成官方授权，即可创建聊天或编程工作区。

`configure_solo.py` 支持 `--profile auto|8g|12g|24g`。自动检测失败可指定与服务器内存匹配的档位。不要运行历史兼容的 `configure.py`、`configure_admin.py` 来初始化此独享版。

## 日常停止与恢复

先在网页停止正在运行的任务和工作区；工作区是应用单独创建的容器，不能只靠 `docker compose stop` 判断任务都停止了。下列路径对应手动安装；一键安装改成 `/opt/claude-cloud-solo`。

```bash
sudo -i
cd /opt/claude-cloud-opensource
docker compose stop
```

恢复网站：

```bash
cd /opt/claude-cloud-opensource
docker compose up -d --wait
```

数据在 Docker 卷中，停止服务不会清空文件。不要添加 `down -v`，也不要执行清空卷的命令。

## 更新

先完成服务器备份或快照，停止网页中的 Claude 任务与工作区。将新源码 ZIP 上传到 `/tmp/claude-cloud-opensource-source.zip`。进入现有安装目录后执行下面命令；它覆盖程序源码，不替换现有 `.env`，不删除数据卷。

```bash
sudo -i
cd /opt/claude-cloud-opensource
CLAUDE_UPDATE_DIR=$(mktemp -d /tmp/claude-cloud-update.XXXXXX)
unzip -q /tmp/claude-cloud-opensource-source.zip -d "$CLAUDE_UPDATE_DIR"
cp -a "$CLAUDE_UPDATE_DIR/claude-cloud-opensource/." .
docker compose --profile tools build worker login-browser
docker compose up -d --build --wait web egress caddy
docker compose ps
```

一键部署的现有目录是 `/opt/claude-cloud-solo`，只需替换上面的 `cd`。保留 `.oneclick.json`、`.oneclick.compose.yaml` 和原始配置。源码更新不会修改原有安装模式，不支持直接把历史多人站点变成独享站点。

网页仍显示旧内容时，先硬刷新或重新打开浏览器。仍不一致，再检查构建日志。只有容器 `up` 而没有重新 `build`，不会把新前端打进旧镜像。

## 修改地址、密码与备份

换 IP 或域名时先完成 DNS、防火墙设置，再运行；会保留现有密码和工作区：

```bash
cd /opt/claude-cloud-opensource
read -rp "新的公网 IP 或域名（不带 https://）: " CLAUDE_ADDRESS
python3 scripts/deploy_public.py "$CLAUDE_ADDRESS"
```

网站密码在网页「管理与设置」修改。修改后所有设备重新登录，文件与 Claude 授权保留。不要把 `.env` 中的哈希改成明文密码。

备份应包括源码版本、私有 `.env`、一键部署标记（如有）、应用数据库卷、授权卷和全部工作区文件／会话卷。仅备份源码 ZIP 不会备份对话或文件。使用云服务器完整快照前先停止任务与容器，以取得一致的数据。备份包含授权资料，应私密保管，不要放入 GitHub。

## 常见问题

在安装目录以 root 运行：

```bash
docker compose ps
docker compose logs --tail=80 web
docker compose logs --tail=80 egress
docker compose logs --tail=80 caddy
```

- **permission denied：**普通用户没有权限读取 root 创建的 `.env` 或操作 Docker。使用 `sudo -i` 后进入安装目录；不要把 `.env` 改成所有人可读。
- **HTTPS 检查失败：**确认地址仍是当前服务器、域名已解析、TCP 80／443 可从公网到达。按 Caddy 日志定位；不要关闭证书验证来掩盖错误。
- **独享配置已存在：**说明初始化已经完成。保持原 `.env`，继续部署或按更新流程操作。
- **没有 Claude 回复：**在网页查看授权和官方额度，再检查 egress 日志。服务器出口需要能正常访问 Claude 服务。
- **聊天模式不能操作文件：**聊天模式禁用了工具。请把文本放进消息，或新建编程工作区进行文件处理。
- **工作区文件丢失：**先确认打开了正确工作区。停止会保留文件，删除工作区则会删除文件与记录。
- **CPU／内存不足：**减少同时运行的工作区与大型构建任务；8 GB 是本项目最低部署档位，不是任意项目的性能保证。

## 从源码运行桌面部署器

在 Windows 安装 Python 3.11+（包含 Tkinter），打开源码根目录：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install paramiko==4.0.0
.\.venv\Scripts\python.exe scripts/build_release.py --payload-only
.\.venv\Scripts\python.exe installer/app.py
```

源码桌面部署器和 EXE 使用同样的 SSH 主机指纹校验、payload 完整性校验和失败重试机制，不需要项目激活码。

## 构建发布包

### 源码 ZIP 与 payload

在仓库根目录运行：

```bash
python scripts/build_release.py
```

得到 `dist/claude-cloud-opensource-source.zip` 和 `installer/payload/`。只准备本地部署器的 payload 可使用 `--payload-only`。

### Windows EXE 与完整发布包

在 Windows 的 PowerShell、源码根目录运行：

```powershell
py -3.11 -m venv installer/.venv
.\installer\.venv\Scripts\python.exe -m pip install -r installer/requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File installer/build.ps1
.\installer\.venv\Scripts\python.exe scripts/build_release.py --windows
```

得到 `dist/claude-cloud-opensource-windows.zip`。完整包包含 EXE、payload、对应源码 ZIP、使用说明和许可证。不要只分发 EXE 或删去对应源码与第三方声明。源码 ZIP 顶层目录固定为 `claude-cloud-opensource/`。

### 本地验证

Python 与 Node.js 22+ 准备好后，在仓库根目录运行：

```bash
python -m pip install -r requirements-dev.txt paramiko==4.0.0
python -m unittest discover -s tests -v
python -m unittest discover -s installer/tests -v
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run check
npm --prefix frontend run build
```

Windows 上可用已创建的虚拟环境 Python 路径代替 `python`。Docker 实际部署与浏览器沙箱验证由 Linux CI 或测试服务器完成；单元测试通过不代表已经验证真实账号登录。