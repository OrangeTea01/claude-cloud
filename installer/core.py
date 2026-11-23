"""SSH deployment engine. GUI-independent and testable without a server."""
from dataclasses import dataclass
import base64
import codecs
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid


PAYLOAD_NAMES = frozenset({'claude-cloud-solo.zip', 'install_solo.py'})
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024


class DeploymentError(Exception):
    pass


class Cancelled(DeploymentError):
    pass


def canonical_host(value):
    value = value.strip()
    if not value or len(value) > 253 or any(c in value for c in '/\\:@?#$\'"%') or any(c.isspace() for c in value):
        raise DeploymentError('服务器地址只能填写公网 IPv4 或完整域名，不带 https://、端口和路径。')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            host = value.rstrip('.').encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise DeploymentError('域名格式不正确。') from None
        parts = host.split('.')
        if len(parts) < 2 or parts[-1].isdigit() or parts[-1] in {'local', 'localhost', 'test', 'example', 'invalid', 'internal', 'onion'} or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in parts):
            raise DeploymentError('请填写完整公网域名。')
        return host
    if address.version != 4 or not address.is_global or address.is_multicast or address.is_reserved:
        raise DeploymentError('请填写公网 IPv4 地址。')
    return str(address)


@dataclass(repr=False)
class Settings:
    host: str
    port: int
    username: str
    auth: str
    ssh_password: str
    key_path: str
    key_passphrase: str
    sudo_password: str
    site_password: str
    address: str
    profile: str = 'auto'

    @classmethod
    def from_form(cls, form):
        host = canonical_host(form.get('host', ''))
        address = canonical_host(form.get('address', '').strip() or host)
        try:
            port = int(form.get('port', '22'))
        except (ValueError, TypeError):
            raise DeploymentError('SSH 端口必须为 1 至 65535 的数字。') from None
        if not 1 <= port <= 65535:
            raise DeploymentError('SSH 端口必须为 1 至 65535。')
        username = form.get('username', '').strip()
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', username):
            raise DeploymentError('请填写 SSH 用户名，例如 root 或 debian。')
        auth = form.get('auth', 'password')
        if auth not in {'password', 'key'}:
            raise DeploymentError('请选择密码或私钥登录。')
        password = form.get('site_password', '')
        if not 12 <= len(password) <= 1024 or any(c in password for c in '\r\n\x00'):
            raise DeploymentError('网站密码至少 12 个字符，最多 1024 个，不能包含换行。')
        if password != form.get('confirm_password', ''):
            raise DeploymentError('两次网站密码不一致。')
        profile = form.get('profile', 'auto')
        if profile not in {'auto', '8g', '12g', '24g'}:
            raise DeploymentError('请选择正确的服务器配置。')
        ssh_password = form.get('ssh_password', '')
        key_path = form.get('key_path', '').strip()
        sudo_password = form.get('sudo_password', '')
        key_passphrase = form.get('key_passphrase', '')
        if auth == 'password' and not ssh_password:
            raise DeploymentError('请填写服务器 SSH 密码。')
        if auth == 'key' and not Path(key_path).is_file():
            raise DeploymentError('请选择本机可读取的 OpenSSH 或 PEM 私钥文件。')
        if any('\n' in v or '\r' in v or '\x00' in v for v in (ssh_password, sudo_password, key_passphrase)):
            raise DeploymentError('服务器密码或私钥口令不能包含换行。')
        return cls(host, port, username, auth, ssh_password, key_path, key_passphrase, sudo_password, password, address, profile)

    def erase_secrets(self):
        self.ssh_password = self.key_passphrase = self.sudo_password = self.site_password = ''


def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode('ascii').rstrip('=')


class LocalState:
    """Public SSH host keys and stable retry identifiers; never stores passwords."""
    def __init__(self, path):
        self.path = Path(path)
        self.data = {'host_keys': {}, 'attempts': {}}
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(value, dict) or set(value) != {'host_keys', 'attempts'} or not all(isinstance(v, dict) for v in value.values()):
                    raise ValueError()
                self.data = value
            except (OSError, ValueError, UnicodeError):
                raise DeploymentError('本机部署记录无法读取。请保留原文件，勿自动忽略服务器指纹记录。') from None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(5) + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(self.data, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def confirm_key(self, hostname, key, confirm):
        record = {'type': key.get_name(), 'key': key.get_base64(), 'fingerprint': fingerprint(key)}
        saved = self.data['host_keys'].get(hostname)
        if saved is not None:
            if saved != record:
                raise DeploymentError('服务器 SSH 指纹与上次不同，已阻止连接。请先向云服务商核实，不能直接覆盖。')
            return
        if not confirm(hostname, record['fingerprint']):
            raise Cancelled('没有确认服务器身份，连接已取消。')
        self.data['host_keys'][hostname] = record
        self.save()

    def attempt(self, settings):
        key = '%s@%s:%s' % (settings.username, settings.host, settings.port)
        value = self.data['attempts'].get(key)
        if value is None:
            value = uuid.uuid4().hex
            self.data['attempts'][key] = value
            self.save()
        if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
            raise DeploymentError('本机安装标识损坏，请联系维护者。')
        return value


def payload_files(base):
    directory = Path(base) / 'payload'
    try:
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        expected = PAYLOAD_NAMES
        if not isinstance(manifest, dict) or set(manifest) != expected:
            raise ValueError()
        files = {}
        for name in sorted(expected):
            digest = manifest[name]
            file = directory / name
            if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) or not file.is_file() or file.stat().st_size > MAX_PAYLOAD_BYTES:
                raise ValueError()
            if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                raise ValueError()
            files[name] = file
        return files, manifest
    except (ValueError, OSError, UnicodeError):
        raise DeploymentError('部署文件缺失或校验失败，请完整解压发布包，保留程序旁的 payload 文件夹。开发者请先生成部署负载。') from None


ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
BOOTSTRAP_PYTHON = """set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
. /etc/os-release
test "$ID" = debian
case "$VERSION_ID" in 12|13) ;; *) exit 41;; esac
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
apt-get update </dev/null
apt-get install -y python3 </dev/null
test -x /usr/bin/python3
printf '%s\\n' 'CC_EVENT {"event":"bootstrap_complete"}'
"""
ROOT_STAGE_WRAPPER = r'''import hashlib, os, pathlib, shutil, stat, subprocess, sys, tempfile
if os.geteuid() != 0:
    raise SystemExit('Root privileges are required.')
os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
source, helper_hash, archive_hash = sys.argv[1:]
directory = pathlib.Path(tempfile.mkdtemp(prefix='.claude-solo-root-', dir='/var/tmp'))
names = ('install_solo.py', 'claude-cloud-solo.zip')
try:
    for name, expected in zip(names, (helper_hash, archive_hash)):
        fd = os.open(pathlib.Path(source) / name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe uploaded file.')
            data = stream.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Upload integrity verification failed.')
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
    result = subprocess.run([sys.executable, '-I', str(directory / names[0]), '--archive', str(directory / names[1])])
    raise SystemExit(result.returncode)
finally:
    for name in names:
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
    directory.rmdir()
'''


def redact(message, settings):
    text = ANSI.sub('', str(message))
    for secret in (settings.ssh_password, settings.key_passphrase, settings.sudo_password, settings.site_password):
        if secret:
            text = text.replace(secret, '[已隐藏]')
    text = re.sub(r'scrypt\$\d+\$\d+\$\d+\$[a-fA-F0-9]+\$[a-fA-F0-9]+', '[密码哈希已隐藏]', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)[:12000]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_health(origin, installation_id):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(origin + '/api/health', timeout=12) as reply:
            body = json.loads(reply.read(4096))
            return reply.status == 200 and isinstance(body, dict) and body.get('ok') is True and body.get('installationId') == installation_id
    except (OSError, ValueError, urllib.error.URLError):
        return False


class Deployer:
    def __init__(self, base, state, emit, confirm_key, *, client_factory=None, health=local_health):
        self.base, self.state, self.emit, self.confirm_key = Path(base), state, emit, confirm_key
        self.client_factory, self.health = client_factory, health
        self.cancelled = threading.Event()
        self.client = None

    def cancel(self):
        self.cancelled.set()
        if self.client:
            self.client.close()

    def check_cancel(self):
        if self.cancelled.is_set():
            raise Cancelled('连接已停止。云端已完成的步骤保留；使用相同信息可继续部署。')

    def connect(self, settings):
        import paramiko
        deployer = self
        class ConfirmHostKey(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                deployer.state.confirm_key(hostname, key, deployer.confirm_key)
                client.get_host_keys().add(hostname, key.get_name(), key)
        client = self.client_factory() if self.client_factory else paramiko.SSHClient()
        client.set_missing_host_key_policy(ConfirmHostKey())
        self.client = client
        try:
            client.connect(settings.host, port=settings.port, username=settings.username,
                           password=settings.ssh_password if settings.auth == 'password' else None,
                           key_filename=settings.key_path if settings.auth == 'key' else None,
                           passphrase=settings.key_passphrase or None, look_for_keys=False, allow_agent=False,
                           timeout=15, auth_timeout=30, banner_timeout=20)
            client.get_transport().set_keepalive(20)
            return client
        except paramiko.AuthenticationException:
            raise DeploymentError('SSH 登录失败，请检查服务器用户名、密码或私钥。') from None
        except paramiko.PasswordRequiredException:
            raise DeploymentError('私钥需要口令，请填写私钥口令。') from None
        except (socket.gaierror, TimeoutError, ConnectionError, paramiko.SSHException, OSError):
            self.check_cancel()
            raise DeploymentError('无法连接 SSH。请检查服务器地址、端口、防火墙和私钥格式。') from None

    def basic(self, command, *, timeout=30):
        self.check_cancel()
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout, get_pty=False)
        stdin.close()
        # Only bounded preflight output is expected here.
        output = stdout.read(8192)
        error = stderr.read(8192)
        code = stdout.channel.recv_exit_status()
        return code, output.decode('utf-8', 'replace'), error.decode('utf-8', 'replace')

    def run(self, settings):
        files = {}
        stage = None
        sftp = None
        try:
            self.check_cancel()
            self.emit('progress', message='校验本地部署文件', step=0, total=6)
            files, manifest = payload_files(self.base)
            self.check_cancel()
            self.emit('progress', message='连接服务器并核对身份', step=0, total=6)
            self.connect(settings)
            self.check_cancel()
            attempt = self.state.attempt(settings)
            stage = '/tmp/claude-cloud-setup-' + attempt + '-' + secrets.token_hex(6)
            code, uid, _ = self.basic('/usr/bin/id -u')
            if code or not uid.strip().isdigit():
                raise DeploymentError('无法确认服务器登录用户。')
            root = uid.strip() == '0'
            if not root:
                code, _, _ = self.basic('/usr/bin/test -x /usr/bin/sudo')
                if code:
                    raise DeploymentError('此用户没有 sudo 工具，请改用 root 登录或先配置 sudo。')
            code, _, _ = self.basic('/usr/bin/test -x /usr/bin/python3')
            if code:
                self.emit('log', message='服务器缺少基础 Python，正在从 Debian 官方软件源自动安装。')
                bootstrap = '/bin/sh -c ' + shlex.quote(BOOTSTRAP_PYTHON)
                if not root:
                    bootstrap = "/usr/bin/sudo -S -p 'CC_SUDO_PROMPT\\n' -- " + bootstrap
                self.stream(bootstrap, None, settings, sudo=not root, bootstrap=True)
            sftp = self.client.open_sftp()
            sftp.mkdir(stage, mode=0o700)
            sftp.chmod(stage, 0o700)
            self.emit('log', message='正在上传已校验的部署包。')
            for name, local in files.items():
                self.check_cancel()
                remote = stage + '/' + name
                with sftp.open(remote, 'wx') as handle:
                    handle.write(local.read_bytes())
                sftp.chmod(remote, 0o600)
            command = '/usr/bin/python3 -I -c ' + shlex.quote(ROOT_STAGE_WRAPPER) + ' ' + ' '.join(shlex.quote(value) for value in
                (stage, manifest['install_solo.py'], manifest['claude-cloud-solo.zip']))
            if not root:
                command = "/usr/bin/sudo -S -p 'CC_SUDO_PROMPT\\n' -- " + command
            config = {'attempt_id': attempt, 'address': settings.address, 'profile': settings.profile,
                      'site_password': settings.site_password, 'payload_sha256': manifest['claude-cloud-solo.zip']}
            result = self.stream(command, config, settings, sudo=not root)
            self.emit('log', message='云端已通过可信 HTTPS 检查；正在从本机复查。')
            local_ok = self.health(result['url'], result['installationId'])
            self.emit('complete', **result, local_ok=local_ok)
            return result
        except DeploymentError:
            self.check_cancel()
            raise
        except Exception:
            self.check_cancel()
            raise DeploymentError('连接或部署步骤中断。请使用相同设置重新开始；不会自动删除云端文件。') from None
        finally:
            # Delete only this attempt's known temporary uploads, never recursive paths.
            if sftp:
                for name in files:
                    try:
                        sftp.remove(stage + '/' + name)
                    except Exception:
                        pass
                try:
                    sftp.rmdir(stage)
                except Exception:
                    pass
                try:
                    sftp.close()
                except Exception:
                    pass
            if self.client:
                self.client.close()

    def stream(self, command, config, settings, *, sudo=False, bootstrap=False):
        channel = self.client.get_transport().open_session(timeout=30)
        channel.settimeout(30)
        channel.exec_command(command)
        started = time.monotonic()
        buffers = {False: '', True: ''}
        decoders = {False: codecs.getincrementaldecoder('utf-8')('replace'), True: codecs.getincrementaldecoder('utf-8')('replace')}
        sent_config, sent_sudo = False, False
        complete, server_error = None, None
        expected_id = str(uuid.UUID(config['attempt_id'])) if config else None
        def line(text):
            nonlocal sent_config, complete, server_error
            if text.startswith('CC_EVENT '):
                try:
                    event = json.loads(text[len('CC_EVENT '):])
                except ValueError:
                    self.emit('log', message='服务器返回了一条无法识别的进度信息。')
                    return
                if not isinstance(event, dict):
                    return
                kind = event.pop('event', '')
                if kind == 'ready':
                    if sent_config or bootstrap or config is None:
                        raise DeploymentError('服务器重复请求部署配置，已停止。')
                    channel.sendall((json.dumps(config, ensure_ascii=False) + '\n').encode('utf-8'))
                    sent_config = True
                    config.pop('site_password', None)
                    channel.shutdown_write()
                elif kind == 'complete':
                    if bootstrap:
                        raise DeploymentError('服务器返回了不符合当前步骤的完成标记。')
                    if (event.get('url') != 'https://' + settings.address or event.get('path') != '/opt/claude-cloud-solo'
                            or event.get('installationId') != expected_id):
                        raise DeploymentError('服务器返回的访问地址与本次设置不一致。')
                    complete = event
                elif kind == 'bootstrap_complete' and bootstrap:
                    complete = {'bootstrap': True}
                elif kind == 'error':
                    server_error = redact(event.get('message', '部署失败。'), settings)
                elif kind in {'log', 'progress'}:
                    event['message'] = redact(event.get('message', ''), settings)
                    self.emit(kind, **event)
            elif text.strip():
                self.emit('log', message=redact(text, settings))
        try:
            while True:
                self.check_cancel()
                if time.monotonic() - started > 3 * 3600:
                    raise DeploymentError('部署超过 3 小时仍未结束，连接已停止。可用相同信息重试。')
                activity = False
                for error_stream in (False, True):
                    ready = channel.recv_stderr_ready if error_stream else channel.recv_ready
                    receive = channel.recv_stderr if error_stream else channel.recv
                    while ready():
                        activity = True
                        data = receive(8192)
                        if not data:
                            break
                        buffers[error_stream] += decoders[error_stream].decode(data)
                        if len(buffers[error_stream]) > 128000:
                            raise DeploymentError('服务器输出异常，连接已停止。')
                        if 'CC_SUDO_PROMPT' in buffers[error_stream]:
                            before, after = buffers[error_stream].split('CC_SUDO_PROMPT', 1)
                            if before.strip():
                                line(before)
                            buffers[error_stream] = after.lstrip('\\n\r\n')
                            if not sudo or sent_sudo or sent_config:
                                raise DeploymentError('sudo 密码未通过或服务器重复索要密码，请检查后重试。')
                            password = settings.sudo_password or (settings.ssh_password if settings.auth == 'password' else '')
                            if not password:
                                raise DeploymentError('该用户需要 sudo 密码，请在高级设置中填写。')
                            channel.sendall((password + '\n').encode('utf-8'))
                            sent_sudo = True
                        while '\n' in buffers[error_stream]:
                            text, buffers[error_stream] = buffers[error_stream].split('\n', 1)
                            line(text.rstrip('\r'))
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if not bootstrap and not sent_config and time.monotonic() - started > 90:
                    raise DeploymentError('服务器未能启动部署程序，请检查 sudo 权限。')
                if not activity:
                    time.sleep(.05)
            for tail in buffers.values():
                if tail.strip():
                    line(tail)
            code = channel.recv_exit_status()
            if code != 0 or not complete:
                raise DeploymentError(server_error or '云端安装没有完成。可查看日志后使用相同设置重试。')
            return complete
        finally:
            channel.close()
