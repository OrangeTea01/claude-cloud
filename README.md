# Claude Cloud 独享开源版

把自己的 Claude Code 放在 Linux 云服务器上，通过手机或电脑浏览器聊天、编程和管理文件。网站只需一个自设密码，管理操作收在同一页面的菜单中。

## 下载与开始

- [Windows 一键部署包](https://github.com/OrangeTea01/claude-cloud-opensource/releases/latest/download/claude-cloud-opensource-windows.zip)：完整解压后双击 `开始部署.exe`。
- [服务器部署源码包](https://github.com/OrangeTea01/claude-cloud-opensource/releases/latest/download/claude-cloud-opensource-source.zip)：手动部署或开发使用。
- [发布版本与 SHA256 校验值](https://github.com/OrangeTea01/claude-cloud-opensource/releases/latest)
- [完整部署命令](DEPLOYMENT.md) · [新手使用教程](BEGINNER_GUIDE.md)

本版不需要本项目激活码，不访问销售站或激活服务。你仍需自行准备云服务器和可使用 Claude Code 的 Claude 账号；服务器和 Claude 服务费用由你承担。此仓库是部署器和网页管理程序的源码，不包含 Claude 官方客户端二进制，也不提供 Claude 账号、额度或模型。

## 主要功能

- 个人独享：没有用户名、分账号、工作区密码或站内个人额度分配。
- 创建工作区时选择「聊天」或「编程」，模式在创建后固定。聊天禁用 Claude 工具，适合问答、写作和图片讨论；编程可在隔离工作区中读写文件、执行命令，通过网页确认工具权限。
- 普通聊天仍使用服务器上的 Claude Code，不是 claude.ai 的全部功能。聊天模式不执行代码、浏览网站或自行读取工作区文件；可以直接发送文字，或附上支持的图片、UTF-8 文本文件，由程序作为消息内容传给 Claude。
- Markdown 回复、文件和图片上传、工作区文件管理、单文件及工作区 ZIP 下载。
- 网页关闭后任务可在服务器继续运行。空闲工作区停止时保留文件和记录；删除工作区才会删除其文件和记录。
- 同一页面内完成 Claude 授权、打开官方注册／订阅／充值页面、查询官方用量和修改网站密码。
- 临时账号浏览器运行在服务器上。实际出网 IP 取决于服务器网络，不保证永久固定 IP。

## 最快开始

**Windows 一键部署：**下载发布附件 `claude-cloud-opensource-windows.zip`，完整解压，双击 `开始部署.exe`。保留旁边的 `payload`，填写服务器、SSH 登录信息和网站密码，核对首次 SSH 指纹后开始。完成后打开程序给出的网址，再在网页中授权自己的 Claude 账号。

**在 Debian 服务器直接安装：**下载 `claude-cloud-opensource-source.zip`，按 [完整部署命令](DEPLOYMENT.md) 依次运行。支持全新 Debian 12／13，需要 Docker Engine 28+、Docker Compose v2 和至少 8 GB 内存。安装器能自动选择 8／12／24 GB 资源档位。

首次准备还需在云控制台开放 SSH 端口、TCP 80 和 443。使用域名时先完成 DNS 解析。首次登录、验证码和支付由你本人在官方页面完成。

## 使用与开发

- [新手开始](BEGINNER_GUIDE.md)
- [页面使用说明](SOLO_GUIDE.md)
- [安装、更新和排错命令](DEPLOYMENT.md)
- [内部 API 与兼容行为](API.md)

从源码启动桌面部署器：安装 Python 3.11+（带 Tkinter）及 `paramiko==4.0.0`，在仓库根目录运行 `python scripts/build_release.py --payload-only`，再运行 `python installer/app.py`。这只启动本地部署面板，不会自动连接服务器。

发布构建脚本默认生成 payload 和 `dist/claude-cloud-opensource-source.zip`；已有 Windows EXE 时，增加 `--windows` 还会生成 `dist/claude-cloud-opensource-windows.zip`。Windows 发布包同时携带对应源码 ZIP、许可证和第三方声明。构建步骤见 [部署文档](DEPLOYMENT.md#构建发布包)。

源码、部署 payload 和 EXE 不能混用不同版本。GitHub Actions 检查 Python 测试、前端构建、部署器和 Docker 隔离；CI 不登录 Claude，也不调用模型。

## 许可证与范围

本项目自有代码采用 **GPL-3.0-only**，见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。第三方组件保留各自许可证，见 [THIRD_PARTY.md](THIRD_PARTY.md)。Claude、Claude Code 及相关商标属于各自权利人；本项目是独立项目，未获其官方背书。

不要提交 `.env`、账号凭据、SSH 私钥、数据库、用户工作区、日志或私人备份。开源许可不改变第三方服务的使用条款与账号限制。
