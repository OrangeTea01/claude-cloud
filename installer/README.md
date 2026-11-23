# Claude Cloud 开源独享版部署程序

完整解压 Windows 发布包，双击 `开始部署.exe`，填写服务器地址、SSH 用户名和密码（或选择私钥）、网站登录密码，点击「开始部署」。没有项目激活码，也不连接销售站验证授权。`payload/` 文件夹必须和 EXE 放在一起。

无激活码指本项目的部署工具。Claude 本身仍需有效账号及相应服务权限；部署完成后在网站菜单里由本人完成官方登录。本程序不会代注册、输入验证码或购买订阅。

支持全新 Debian 12 / 13 服务器，8 GB 内存规格起、15 GiB 可用磁盘。root 可直接安装；其他 SSH 用户必须拥有 sudo 权限。云防火墙放行 SSH 及 TCP 80、443。域名须提前解析到服务器；不填域名则尝试为公网 IP 配置 HTTPS。程序不能修改云控制台防火墙或 DNS。

网站密码至少 12 个字符。SSH 密码、sudo 密码、私钥口令和网站密码不写入本机状态或日志，也不通过远程命令参数传输；任务结束后清空界面。服务器仅保存网站密码哈希。首次连接需核对 SSH 公钥指纹；指纹变化时停止，不能直接覆盖。

部署目录是 `/opt/claude-cloud-solo`。安装前校验随包两份部署文件的 SHA256，SSH 上传后在提权前后再次检查。未知目录、已有站点和不属于本次安装的数据卷不会自动覆盖。SHA256 检查能发现损坏和文件错配，不代替对下载来源的核实。

部署失败后，使用**同一电脑、同一 SSH 地址/端口/用户名、同一版本部署包、相同网站地址和第一次的网站密码**重新开始。重试标识保存在 `%LOCALAPPDATA%/ClaudeCloudOpenSourceInstaller/state.json`，与 SSH 公钥记录一起保留。此文件不含密码。若更换电脑，需安全迁移这份记录；缺少原记录时程序不会接管现有安装。不要为了重试删除状态文件或云端安装标记。

「停止连接」会断开 SSH，云端已经开始的步骤可能继续。不会自动卸载或删除工作区；再次开始时由服务器进程锁防止并行安装。结束时只清理本次临时上传，随包 `payload/` 保留。

程序在服务器确认可信 HTTPS 和应用健康后显示网址，并从本机复查。完成后用网站密码登录，在菜单中连接 Claude，即可创建聊天或编程工作区。

## 从源码运行

在项目根目录的 Windows PowerShell 依次运行：

```powershell
python -m venv installer/.venv
installer/.venv/Scripts/python.exe -m pip install -r installer/requirements.txt
python scripts/build_release.py --payload-only
installer/.venv/Scripts/python.exe -m unittest discover -s installer/tests -v
installer/.venv/Scripts/python.exe -m installer.app
```

开发运行从 `installer/payload/` 读取负载；EXE 从自身所在目录的 `payload/` 读取。

## 构建 Windows 发布包

在同一项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File installer/build.ps1
python scripts/build_release.py --windows
```

构建脚本也支持 `-Python C:\path\to\python.exe` 指定装有 `installer/requirements.txt` 依赖的 Python。PyInstaller 生成 `installer/dist/开始部署.exe`；发布脚本将部署文件、说明、许可证及对应源码一同整理。不要只发单独的 EXE，否则会提示缺少负载。

项目自有代码采用 GPL-3.0-only。分发二进制时须履行 GPL 的对应源码要求；保留根目录许可证及 `THIRD_PARTY_NOTICES.txt`。第三方依赖、运行时和 Claude 客户端分别遵循其自身许可。

`payload/manifest.json` 格式如下，每个文件最多 32 MiB；网站 ZIP 内统一使用 `claude-cloud/` 前缀，不能含 `.env`、数据库或依赖目录：

```json
{
  "claude-cloud-solo.zip": "64位SHA256",
  "install_solo.py": "64位SHA256"
}
```
