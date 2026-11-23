"""Fresh-server solo provisioning. Configuration arrives only through SSH stdin.

This file deliberately uses only the Python standard library. It is copied to a
private SFTP staging directory and executed as root by the Windows installer.
"""
import argparse
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import signal
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile

TARGET = Path('/opt/claude-cloud-solo')
MARKER = '.oneclick.json'
OVERRIDE = '.oneclick.compose.yaml'
INSTALL_LABEL = 'io.claude-cloud.installation'
PROFILES = {
    '8g': ('1536m', '1', '20', '3'),
    '12g': ('2g', '1', '20', '3'),
    '24g': ('3g', '1.5', '30', '5'),
}


class InstallError(Exception):
    pass


def emit(event, **fields):
    print('CC_EVENT ' + json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)


def public_address(value):
    if not isinstance(value, str) or not value or len(value) > 253 or any(c in value for c in '/\\:@?#$\'"%'):
        raise InstallError('网站地址必须是公网 IPv4 或完整域名，不带 https://、端口或路径。')
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        try:
            host = value.rstrip('.').encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise InstallError('网站域名格式不正确。') from None
        parts = host.split('.')
        if len(host) > 253 or len(parts) < 2 or parts[-1].isdigit() or parts[-1] in {'local', 'localhost', 'internal', 'invalid', 'example', 'test', 'onion'} or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in parts):
            raise InstallError('网站域名格式不正确。')
        return host
    if ip.version != 4 or not ip.is_global or ip.is_multicast or ip.is_reserved:
        raise InstallError('请填写公网 IPv4 地址。')
    return str(ip)


def parse_config(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise InstallError('部署配置读取失败，请重新开始。') from None
    keys = {'attempt_id', 'address', 'profile', 'site_password', 'payload_sha256'}
    if not isinstance(value, dict) or set(value) != keys:
        raise InstallError('部署配置字段不匹配。')
    if not isinstance(value['attempt_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', value['attempt_id']):
        raise InstallError('部署标识无效。')
    if not isinstance(value['payload_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', value['payload_sha256']):
        raise InstallError('部署包校验信息无效。')
    if not isinstance(value['profile'], str) or value['profile'] not in {'auto', *PROFILES}:
        raise InstallError('服务器配置档位无效。')
    password = value['site_password']
    if not isinstance(password, str) or not 12 <= len(password) <= 1024 or '\x00' in password or '\r' in password or '\n' in password:
        raise InstallError('网站密码需为 12 至 1024 个字符，且不含换行。')
    value['address'] = public_address(value['address'])
    return value


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=64)
    return 'scrypt$16384$8$1$%s$%s' % (salt.hex(), digest.hex())


def verify_password(password, encoded):
    try:
        fields = encoded.split('$')
        if len(fields) != 6 or fields[:4] != ['scrypt', '16384', '8', '1'] or len(fields[4]) != 32 or len(fields[5]) != 128:
            return False
        return hmac.compare_digest(encoded, password_hash(password, bytes.fromhex(fields[4])))
    except (ValueError, TypeError):
        return False


def read_regular(path, maximum=1024 * 1024):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise InstallError('部署路径包含不安全的文件链接，已停止。')
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise InstallError('部署文件在检查时发生变化，已停止。')
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise InstallError('部署文件超过允许大小。')
    return data


def write_private(path, body, exclusive=False):
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        fd, name = tempfile.mkstemp(prefix='.oneclick-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def select_profile(profile, mem_kib):
    gib = mem_kib / 1024 / 1024
    if gib < 6:
        raise InstallError('此一键包需要至少 8 GB 规格的服务器；检测到可用总内存不足 6 GiB。')
    chosen = profile if profile != 'auto' else ('24g' if gib >= 20 else '12g' if gib >= 10 else '8g')
    if gib < {'8g': 6, '12g': 10, '24g': 20}[chosen]:
        raise InstallError('所选配置档位超过当前服务器内存，请选择自动。')
    return chosen


def render_env(config, profile, encoded):
    memory, cpus, saved, running = PROFILES[profile]
    values = ["# Private solo installation; do not share this file.", 'APP_MODE=solo',
              "ADMIN_PASSWORD_HASH='" + encoded + "'", 'MANAGEMENT_PASSWORD_HASH=',
              'PUBLIC_ORIGIN=https://' + config['address'], 'DOMAIN=' + config['address'],
              'COOKIE_SECURE=true', 'COMPOSE_PROFILES=https',
              'INSTALLATION_ID=' + str(uuid.UUID(config['attempt_id'])),
              'COMPOSE_FILE=compose.yaml:' + OVERRIDE,
              'WORKER_IMAGE=claude-cloud-worker:local', 'CLAUDE_VERSION=stable',
              'LOGIN_BROWSER_IMAGE=claude-cloud-login-browser:local',
              'WORKSPACE_MEMORY=' + memory, 'WORKSPACE_CPUS=' + cpus,
              'MAX_WORKSPACES=' + saved, 'MAX_RUNNING_WORKSPACES=' + running, '']
    return '\n'.join(values)


def stage_source(archive_path, digest, staging):
    # Entire source archive is small; enforce limits before creating anything.
    raw = read_regular(archive_path, 32 * 1024 * 1024)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
        raise InstallError('上传的部署包校验失败，未安装。')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if not 1 <= len(entries) <= 3000 or sum(i.file_size for i in entries) > 100 * 1024 * 1024:
            raise InstallError('部署包体积或文件数量异常。')
        seen = set()
        for item in entries:
            name = PurePosixPath(item.filename)
            raw_parts = item.filename.rstrip('/').split('/')
            mode = stat.S_IFMT(item.external_attr >> 16)
            if (not item.filename.startswith('claude-cloud/') or '\\' in item.filename or name.is_absolute()
                    or any(p in ('', '..', '.') for p in raw_parts) or item.filename in seen
                    or mode not in (0, stat.S_IFREG, stat.S_IFDIR) or '\x00' in item.filename):
                raise InstallError('部署包包含不安全路径，未解压。')
            if any(p in {'.env', '.git', 'node_modules'} for p in name.parts):
                raise InstallError('部署包不应携带密码、版本库或本地依赖。')
            seen.add(item.filename)
        required = {'claude-cloud/compose.yaml', 'claude-cloud/scripts/deploy_public.py', 'claude-cloud/backend/app.py'}
        if not required <= seen:
            raise InstallError('部署包缺少必要文件。')
        archive.extractall(staging)
    return staging / 'claude-cloud'


def resume_config(target, config):
    try:
        marker = json.loads(read_regular(target / MARKER, 8192))
    except (FileNotFoundError, ValueError):
        raise InstallError('目标目录已有其他安装。为保留原数据，一键安装不会覆盖它。') from None
    if (not isinstance(marker, dict) or marker.get('attempt_id') != config['attempt_id'] or marker.get('payload_sha256') != config['payload_sha256']
            or marker.get('address') != config['address'] or marker.get('kind') != 'claude-cloud-solo-v1'):
        raise InstallError('目标目录属于另一部署或安装包版本。为保留原数据，已停止。')
    if (not isinstance(marker.get('profile'), str) or marker['profile'] not in PROFILES
            or type(marker.get('docker_preexisting')) is not bool or marker.get('state') not in ('configured', 'ready')):
        raise InstallError('已有安装记录损坏。请保留文件并联系维护者，不要删除或覆盖原数据。')
    content = read_regular(target / '.env').decode('utf-8').replace('\r\n', '\n')
    match = re.search(r"(?m)^ADMIN_PASSWORD_HASH='(scrypt\$[^']+)'$", content)
    if 'APP_MODE=solo\n' not in content or not match or not verify_password(config['site_password'], match[1]):
        raise InstallError('继续部署需填写第一次部署时的网站密码，且目标必须是本工具创建的独享版。')
    return marker


def compose_override(attempt):
    labels = {INSTALL_LABEL: attempt}
    services = {name: {'labels': labels} for name in ('web', 'egress', 'caddy')}
    services['web']['environment'] = {'INSTALLATION_ID': str(uuid.UUID(attempt))}
    return json.dumps({'services': services,
                       'volumes': {name: {'labels': labels} for name in ('app_data', 'caddy_data', 'caddy_config')},
                       'networks': {name: {'labels': labels} for name in ('control', 'outbound', 'translation')}}, indent=2)


def check_owned_resources(target, attempt):
    """Never adopt a different installation that appeared while ours was stopped."""
    expected = {
        'container': {'claude-cloud-egress', 'claude-cloud-web-1', 'claude-cloud-caddy-1'},
        'volume': {'claude-cloud_app_data', 'claude-cloud_caddy_data', 'claude-cloud_caddy_config'},
        'network': {'claude-cloud_control', 'claude-cloud_outbound', 'claude-cloud_translation'},
    }
    for kind, names in expected.items():
        listing = ['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=claude-cloud', '--format', '{{.Names}}'] if kind == 'container' else [
            'docker', kind, 'ls', '--filter', 'label=com.docker.compose.project=claude-cloud', '--format', '{{.Name}}']
        result = subprocess.run(listing, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise InstallError('无法检查已有 Docker 资源归属，未启动网站。')
        for name in names | set(result.stdout.splitlines()):
            found = subprocess.run(['docker', kind, 'inspect', name], capture_output=True, text=True, timeout=30)
            if found.returncode:
                # Absence is allowed; daemon/permission failures are not.
                if 'no such' in found.stderr.lower() or 'not found' in found.stderr.lower():
                    continue
                raise InstallError('无法检查已有 Docker 资源归属，未启动网站。')
            try:
                info = json.loads(found.stdout)[0]
                labels = (info.get('Config', {}).get('Labels') if kind == 'container' else info.get('Labels')) or {}
            except (ValueError, IndexError, TypeError):
                raise InstallError('Docker 资源检查结果无效。') from None
            if labels.get(INSTALL_LABEL) != attempt:
                raise InstallError('发现属于其他部署的 Docker %s，本工具不会接管或覆盖它。' % kind)
            if kind == 'container' and labels.get('com.docker.compose.project.working_dir') != str(target):
                raise InstallError('发现配置目录不匹配的 Docker 容器，已停止。')


def run(args, *, cwd=None, timeout=1800, check=True):
    # No shell is used. Fixed command arguments never contain website secrets.
    emit('log', message='正在执行：' + ' '.join(str(v) for v in args[:3]))
    process = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors='replace', start_new_session=True,
                               env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'a'})
    # A watchdog prevents a silent child from surviving forever.
    import threading
    timed_out = threading.Event()
    forced_kill = None
    def kill_group():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    def kill_late():
        nonlocal forced_kill
        timed_out.set()
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        forced_kill = threading.Timer(2, kill_group)
        forced_kill.daemon = True
        forced_kill.start()
    timer = threading.Timer(timeout, kill_late)
    timer.daemon = True
    timer.start()
    try:
        for line in iter(lambda: process.stdout.readline(8192), ''):
            emit('log', message=line.rstrip()[:3000])
        code = process.wait(timeout=10)
    finally:
        timer.cancel()
        if timed_out.is_set():
            kill_group()
        if forced_kill:
            forced_kill.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
    if timed_out.is_set():
        raise InstallError('此步骤等待超时。可用相同设置重新开始，继续未完成部署。')
    if check and code:
        raise InstallError('安装步骤未完成（退出码 %s）。请查看上方日志；修复网络后可重新开始。' % code)
    return code


def preflight(target, resuming):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise InstallError('云端安装程序必须使用 root 权限运行。')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID', '').strip('"') != 'debian' or release.get('VERSION_ID', '').strip('"') not in {'12', '13'}:
        raise InstallError('此安装包仅支持 Debian 12 / 13 全新服务器。')
    if os.uname().machine not in {'x86_64', 'aarch64'}:
        raise InstallError('服务器需为 x86_64 或 ARM64。')
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise InstallError('目标路径不是安全目录。')
    if shutil.disk_usage('/opt').free < 15 * 1024 ** 3:
        raise InstallError('服务器磁盘剩余空间不足 15 GiB。')
    if not resuming:
        if not shutil.which('docker') and any(p.exists() or p.is_symlink() for p in (
                Path('/etc/apt/keyrings/docker.asc'), Path('/etc/apt/sources.list.d/docker.sources'),
                Path('/etc/apt/sources.list.d/docker.list'))):
            raise InstallError('服务器已有未完成的 Docker 软件源设置，请先完成 Docker 安装后重试。')
        for port in (80, 443, 8000):
            with socket.socket() as sock:
                try:
                    sock.bind(('0.0.0.0', port))
                except OSError:
                    raise InstallError('端口 %s 已被其他服务使用。本工具不会停止其他网站。' % port) from None
        if shutil.which('docker'):
            check = subprocess.run(['docker', 'ps', '-a', '--format', '{{.Names}}'], capture_output=True, text=True)
            if check.returncode == 0 and any(n.startswith('claude-cloud') for n in check.stdout.splitlines()):
                raise InstallError('服务器已有 Claude Cloud 容器，不能当成全新服务器安装。')
            volumes = subprocess.run(['docker', 'volume', 'ls', '--format', '{{.Name}}'], capture_output=True, text=True)
            if volumes.returncode == 0 and 'claude-cloud_app_data' in volumes.stdout.splitlines():
                raise InstallError('服务器已有 Claude Cloud 数据卷，一键安装不会接管旧数据。')
    mem = re.search(r'^MemTotal:\s+(\d+)\s+kB', Path('/proc/meminfo').read_text(), re.M)
    if not mem:
        raise InstallError('无法读取服务器内存大小。')
    return int(mem[1]), release.get('VERSION_CODENAME', '').strip('"')


def install_docker(codename, managed=False):
    if shutil.which('docker'):
        if managed:
            run(['systemctl', 'enable', '--now', 'docker'], timeout=120, check=False)
        # Reuse sufficiently recent official Docker, never uninstall packages.
        version = subprocess.run(['docker', 'version', '--format', '{{.Server.Version}}'], capture_output=True, text=True)
        match = re.match(r'(\d+)\.', version.stdout)
        if version.returncode == 0 and match and int(match[1]) >= 28:
            run(['docker', 'compose', 'version'], timeout=30)
            return
        if not managed:
            raise InstallError('已有 Docker 无法运行或版本低于 28，请先手动检查；本工具不会替换现有 Docker。')
    if codename not in {'bookworm', 'trixie'}:
        raise InstallError('无法识别 Debian 软件源名称。')
    if managed:
        run(['dpkg', '--configure', '-a'], timeout=900)
    run(['apt-get', 'update'], timeout=600)
    run(['apt-get', 'install', '-y', 'ca-certificates', 'curl', 'python3'], timeout=900)
    Path('/etc/apt/keyrings').mkdir(mode=0o755, exist_ok=True)
    key = Path('/etc/apt/keyrings/docker.asc')
    if key.is_symlink() or (key.exists() and not key.is_file()):
        raise InstallError('Docker 密钥路径不是安全普通文件，已停止。')
    fd, key_temporary = tempfile.mkstemp(prefix='.docker-', dir=key.parent)
    os.close(fd)
    try:
        run(['curl', '--fail', '--show-error', '--silent', '--proto', '=https', '--tlsv1.2', '--max-time', '90',
             'https://download.docker.com/linux/debian/gpg', '-o', key_temporary], timeout=100)
        downloaded = read_regular(Path(key_temporary), 65536)
        if b'BEGIN PGP PUBLIC KEY BLOCK' not in downloaded:
            raise InstallError('Docker 官方密钥下载结果不正确。')
        os.chmod(key_temporary, 0o644)
        os.replace(key_temporary, key)
    finally:
        if os.path.exists(key_temporary):
            os.unlink(key_temporary)
    arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    if arch not in {'amd64', 'arm64'}:
        raise InstallError('不支持此服务器架构。')
    source = Path('/etc/apt/sources.list.d/docker.sources')
    source_text = 'Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' % (codename, arch)
    if source.exists() or source.is_symlink():
        if read_regular(source).decode('utf-8') != source_text:
            raise InstallError('已有 Docker 软件源与本工具不一致，请先检查，未覆盖。')
    else:
        write_private(source, source_text, exclusive=True)
    source.chmod(0o644)
    run(['apt-get', 'update'], timeout=600)
    run(['apt-get', 'install', '-y', 'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin', 'docker-compose-plugin'], timeout=1200)
    run(['systemctl', 'enable', '--now', 'docker'], timeout=120)
    run(['docker', 'version'], timeout=45)
    run(['docker', 'compose', 'version'], timeout=45)


def local_server_health(address, installation_id):
    """Verify THIS Caddy, with the public hostname/IP and normal CA trust."""
    context = ssl.create_default_context()
    with socket.create_connection(('127.0.0.1', 443), timeout=8) as raw:
        with context.wrap_socket(raw, server_hostname=address) as connection:
            connection.settimeout(8)
            request = 'GET /api/health HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n' % address
            connection.sendall(request.encode('ascii'))
            response = http.client.HTTPResponse(connection)
            response.begin()
            body = json.loads(response.read(4096))
            return response.status == 200 and isinstance(body, dict) and body.get('ok') is True and body.get('installationId') == installation_id


def public_instance_health(origin, installation_id):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(origin + '/api/health', timeout=8) as response:
        body = json.loads(response.read(4096))
        return response.status == 200 and isinstance(body, dict) and body.get('ok') is True and body.get('installationId') == installation_id


def provision(config, archive_path, target=TARGET):
    target = Path(target)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise InstallError('目标路径不是安全目录。')
    resuming = target.exists()
    marker = resume_config(target, config) if resuming else None
    mem, codename = preflight(target, resuming)
    profile = marker['profile'] if marker else select_profile(config['profile'], mem)
    emit('progress', step=1, total=6, message='服务器检查通过，使用 ' + profile + ' 配置')
    if not resuming:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.claude-solo-', dir=target.parent) as staging:
            project = stage_source(archive_path, config['payload_sha256'], Path(staging))
            encoded = password_hash(config['site_password'])
            write_private(project / '.env', render_env(config, profile, encoded), exclusive=True)
            write_private(project / OVERRIDE, compose_override(config['attempt_id']), exclusive=True)
            marker = {'kind': 'claude-cloud-solo-v1', 'attempt_id': config['attempt_id'], 'payload_sha256': config['payload_sha256'],
                      'address': config['address'], 'profile': profile, 'state': 'configured',
                      'docker_preexisting': bool(shutil.which('docker'))}
            write_private(project / MARKER, json.dumps(marker), exclusive=True)
            # Atomic publication of fully configured source; target never overwritten.
            if target.exists():
                raise InstallError('目标目录被其他程序创建，未覆盖。')
            os.rename(project, target)
    else:
        emit('log', message='已核对之前的安装标识和网站密码，继续未完成步骤。')
    config.pop('site_password', None)
    emit('progress', step=2, total=6, message='安装或检查 Docker')
    install_docker(codename, managed=not marker.get('docker_preexisting', True))
    # An incomplete installation cannot use its marker to take over a later site.
    if read_regular(target / OVERRIDE).decode('utf-8') != compose_override(config['attempt_id']):
        raise InstallError('安装专用 Docker 配置已变化，未覆盖或启动。')
    check_owned_resources(target, config['attempt_id'])
    # Existing strict HTTPS helper keeps hostname/certificate checks enabled.
    emit('progress', step=3, total=6, message='构建 Claude 和服务器浏览器，首次可能需要较长时间')
    run(['docker', 'compose', '--profile', 'tools', 'build', 'worker', 'login-browser'], cwd=target, timeout=3600)
    emit('progress', step=4, total=6, message='构建网页并启动 HTTPS 服务')
    run(['docker', 'compose', '--profile', 'https', 'up', '-d', '--build', '--wait', '--wait-timeout', '240', 'web', 'egress', 'caddy'], cwd=target, timeout=3600)
    emit('progress', step=5, total=6, message='等待可信 HTTPS 证书和网站健康检查')
    sys.path.insert(0, str(target / 'scripts'))
    from deploy_public import wait_for_https
    origin = 'https://' + config['address']
    error = wait_for_https(origin)
    if error:
        run(['docker', 'compose', 'logs', '--tail=35', 'caddy'], cwd=target, timeout=30, check=False)
        raise InstallError('HTTPS 尚未就绪：' + error + '。请检查域名解析及云防火墙 TCP 80、443，再用相同信息重试。')
    try:
        installation_id = str(uuid.UUID(config['attempt_id']))
        own_server_ready = (local_server_health(config['address'], installation_id)
                            and public_instance_health(origin, installation_id))
    except (OSError, ValueError, http.client.HTTPException):
        own_server_ready = False
    if not own_server_ready:
        raise InstallError('公网地址可响应，但本机 Caddy 未通过该地址的可信证书检查。请核对域名或公网 IP 是否确实属于本服务器，再重试。')
    marker['state'] = 'ready'
    write_private(target / MARKER, json.dumps(marker))
    emit('progress', step=6, total=6, message='网站部署完成')
    emit('complete', url=origin, path=str(target), profile=profile, installationId=installation_id,
         message='网站已就绪。用设置的网站密码登录，然后在网页中连接 Claude。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True)
    args = parser.parse_args()
    emit('ready')
    raw = sys.stdin.buffer.readline(16385)
    if len(raw) > 16384:
        raise InstallError('部署配置过大。')
    config = parse_config(raw)
    # No configuration is printed; the only password copy is held in memory.
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise InstallError('云端安装程序必须使用 root 权限运行。')
    import fcntl
    fd = os.open('/opt/.claude-cloud-solo-install.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('上一次云端部署仍在运行。请等待完成后再重试；不要同时重复部署。') from None
        provision(config, Path(args.archive))


if __name__ == '__main__':
    try:
        main()
    except InstallError as error:
        emit('error', message=str(error))
        sys.exit(1)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError):
        emit('error', message='服务器步骤失败，请保留当前设置并重试；原有数据没有被自动删除。')
        sys.exit(1)
