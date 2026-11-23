"""A private, fixed-purpose Claude OAuth environment for official usage queries."""
import asyncio
from dataclasses import replace
import json

from .docker_backend import BrokerError, DockerBroker, RESOURCE_LABEL


ADMIN_USAGE_ID = "admin-usage-auth"

# Never inherit an inference token, API credential, custom endpoint or another
# configuration directory. The official CLI alone reads/refreshes this home.
PRIVATE_ENV = r'''import os
environment = {key: value for key, value in os.environ.items()
               if not key.startswith(('ANTHROPIC_', 'CLAUDE_CODE_OAUTH_', 'CLAUDE_CODE_USE_'))}
for key in ('CLAUDE_CODE_SIMPLE', 'CLAUDE_CODE_BARE', 'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR'):
    environment.pop(key, None)
environment['HOME'] = '/home/claude'
environment['CLAUDE_CONFIG_DIR'] = '/home/claude/.claude'
'''
LAUNCH_PRIVATE = PRIVATE_ENV + r'''
import sys
os.execve('/usr/local/bin/claude', ['/usr/local/bin/claude', *sys.argv[1:]], environment)
'''

# Only booleans and a bounded administrator-visible email leave this helper.
# In particular neither the credential file nor raw auth-status output does.
CREDENTIAL_STATUS = PRIVATE_ENV + r'''
import json, stat, subprocess
result = {'authenticated': False, 'profileScope': False, 'accountEmail': None}
try:
    fd = os.open('/home/claude/.claude/.credentials.json', os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
            raise ValueError()
        credentials = json.loads(source.read(1048577))
    oauth = credentials.get('claudeAiOauth', {})
    scopes = oauth.get('scopes', [])
    result['profileScope'] = isinstance(scopes, list) and 'user:profile' in scopes
    material = all(isinstance(oauth.get(key), str) and 1 <= len(oauth[key]) <= 32768
                   for key in ('accessToken', 'refreshToken'))
    process = subprocess.run(['/usr/local/bin/claude', 'auth', 'status'],
                             env=environment, capture_output=True, timeout=8)
    if process.returncode == 0 and len(process.stdout) <= 16384:
        status = json.loads(process.stdout)
        result['authenticated'] = bool(status.get('loggedIn') is True
                                       and status.get('authMethod') == 'claude.ai'
                                       and result['profileScope'] and material)
        email = status.get('email')
        if (result['authenticated'] and isinstance(email, str) and 3 <= len(email) <= 254
                and '@' in email and not any(ord(char) < 32 or ord(char) == 127 for char in email)):
            result['accountEmail'] = email
except Exception:
    pass
print(json.dumps(result, separators=(',', ':')))
'''


class AdminUsageBroker(DockerBroker):
    """Outside the workspace registry and without the shared auth volume."""
    workspace_id = ADMIN_USAGE_ID

    def __init__(self, parent):
        self.parent = parent
        self.config = replace(parent.config, memory="512m", cpus="1")
        self.owner = parent.owner
        self.command = parent.command
        self.lock = asyncio.Lock()
        self._engine_checked = False
        self._policy_checked_workers = set()
        self._uses_shared_auth = False

    def get(self, workspace_id):
        if workspace_id != self.workspace_id:
            raise BrokerError("管理员额度授权环境不存在", 404)
        return {"id": self.workspace_id, "name": "管理员额度查询授权", "createdAt": None}

    async def list(self):
        return [await self.workspace(self.workspace_id)]

    def labels(self, workspace_id):
        self.get(workspace_id)
        return [*super().labels(workspace_id), "--label", f"{RESOURCE_LABEL}={ADMIN_USAGE_ID}"]

    async def inspect_owned(self, kind, name, workspace_id):
        self.get(workspace_id)
        info = await super().inspect_owned(kind, name, workspace_id)
        if info:
            labels = (info.get("Config", {}).get("Labels") if kind == "container" else info.get("Labels")) or {}
            if labels.get(RESOURCE_LABEL) != ADMIN_USAGE_ID:
                raise BrokerError("管理员额度授权环境归属不匹配", 409)
        return info

    async def _ensure_model_policy(self):
        await self.parent._ensure_model_policy()

    def _exec(self, interactive=False):
        return ["docker", "exec", "-it" if interactive else "-i", "--user", "1000:1000",
                "--workdir", "/workspace", self.names(self.workspace_id)["container"]]

    async def terminal_command(self, workspace_id):
        self.get(workspace_id)
        await self.start(workspace_id)
        return [*self._exec(True), "/usr/bin/timeout", "--signal=TERM", "--kill-after=5s", "30m",
                "/usr/bin/python3", "-I", "-c", LAUNCH_PRIVATE, "auth", "login", "--claudeai"]

    async def auth_status(self):
        code, output, _ = await self.command(
            *self._exec()[1:], "/usr/bin/timeout", "--signal=TERM", "--kill-after=2s", "12s",
            "/usr/bin/python3", "-I", "-c", CREDENTIAL_STATUS, timeout=15, check=False)
        try:
            if code or len(output) > 4096:
                raise ValueError()
            state = json.loads(output)
            email = state.get("accountEmail")
            email = email if (isinstance(email, str) and 3 <= len(email) <= 254 and "@" in email
                              and not any(ord(c) < 32 or ord(c) == 127 for c in email)) else None
            return {"authenticated": state.get("authenticated") is True,
                    "profileScope": state.get("profileScope") is True, "accountEmail": email}
        except (ValueError, TypeError, AttributeError):
            raise BrokerError("未能确认额度查询授权，请重新登录", 503) from None

    def usage_command(self):
        return [*self._exec(), "/usr/bin/timeout", "--signal=TERM", "--kill-after=5s", "25s",
                "/usr/bin/python3", "-I", "-c", LAUNCH_PRIVATE,
                "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "default", "--permission-prompt-tool", "stdio",
                "--safe-mode", "--restricted", "--tools", "", "--strict-mcp-config", "--no-session-persistence"]

    async def reconcile(self):
        name = self.names(self.workspace_id)["container"] + "-login"
        info = await self.inspect_owned("container", name, self.workspace_id)
        if info:
            if info.get("Config", {}).get("Labels", {}).get("io.claude-cloud.role") != "login-browser":
                raise BrokerError("管理员额度登录浏览器归属不匹配", 409)
            await self.command("container", "rm", "--force", name)
        await self.stop(self.workspace_id)

    def close(self):
        # The parent owns the registry; private credentials remain in its home.
        pass
