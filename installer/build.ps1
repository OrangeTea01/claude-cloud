param([string]$Python = '')

$ErrorActionPreference = 'Stop'
$InstallerRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallerPython = if ($Python) { (Resolve-Path -LiteralPath $Python).Path } else { Join-Path $InstallerRoot '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $InstallerPython)) { throw '先使用 Python 3.11 创建 installer/.venv 并安装 requirements.txt。' }
& $InstallerPython -m PyInstaller --noconfirm --clean --onefile --windowed --paths $InstallerRoot --name '开始部署' --distpath (Join-Path $InstallerRoot 'dist') --workpath (Join-Path $InstallerRoot '.build') --specpath $InstallerRoot (Join-Path $InstallerRoot 'app.py')
if ($LASTEXITCODE -ne 0) { throw 'Windows 程序构建失败。' }
Write-Output '开源版 Windows EXE 已构建至 installer/dist/开始部署.exe，无需激活码。请在项目根目录运行 python scripts/build_release.py --windows，生成附带 payload、许可证与对应源码的完整发布包。'
