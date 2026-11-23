# 第三方组件与声明

项目自有代码采用 GPL-3.0-only，见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。下列组件继续适用其原许可证；本项目的许可证不替代第三方许可证。

## 仓库中随附的材料

- **noVNC**：页面使用 npm 包，版本由 `frontend/package-lock.json` 固定。源项目为 <https://github.com/novnc/noVNC>。完整声明、作者列表及 MPL、BSD、OFL、Pako 等许可证保存在 [frontend/public/novnc-licenses](frontend/public/novnc-licenses/)，前端发布时一并保留。
- **Playwright 的 Chromium 容器 seccomp 配置**：来源、版本及本项目修改记录见 [deploy/login_browser/THIRD_PARTY.md](deploy/login_browser/THIRD_PARTY.md)，Apache-2.0 全文见 [LICENSE.playwright](deploy/login_browser/LICENSE.playwright)。
- **Windows 部署器依赖与运行时**：Paramiko、PyInstaller、密码学库、Python、Tcl/Tk 等声明见 [installer/THIRD_PARTY_NOTICES.txt](installer/THIRD_PARTY_NOTICES.txt)。发布 EXE 时必须保留该文件；具体版本见 `installer/requirements.txt`。

## 安装或构建时取得的依赖

Python 依赖由 `requirements*.txt` 指定，前端依赖由 `frontend/package*.json` 指定。Docker 基础镜像及 Linux 软件包由 Dockerfile 声明。它们的源码、许可证与版权声明由各自上游提供；重新分发构建产物时应保留适用的声明并履行对应义务。

Claude 官方客户端在构建工作区镜像时从官方安装入口取得，未放入本项目源码发布包。Claude 服务、账号、客户端和商标不属于本项目的开源授权范围。使用者需自行拥有相应服务访问权限并遵守其条款。

## GPL 文本来源

根目录 `LICENSE` 是 GNU 发布的完整 GPL 第 3 版正文：<https://www.gnu.org/licenses/gpl-3.0.txt>。本项目选择 **GPL-3.0-only**，不自动授权采用后续版本。