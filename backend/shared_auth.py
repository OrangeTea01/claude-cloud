"""Fixed helpers for an administrator's inference-only Claude subscription token.

The token is never a Docker argument or a Docker Config.Env value. A worker's
official CLI must be able to read it, so this is not a secret boundary against
arbitrary code approved to run as that same worker user.
"""
import re

AUTH_MOUNT = "/run/claude-cloud-auth"
TOKEN_PATTERN = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{64,4096}\Z")


def validate_shared_token(token):
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("请使用 Claude setup-token 生成的完整授权令牌")
    return token


AUTH_HELPER = r'''import json, os, re, stat, sys, tempfile
request = json.load(sys.stdin)
operation = request.get('operation')
path = '/auth/token'
pattern = re.compile(r'sk-ant-oat01-[A-Za-z0-9_-]{64,4096}\Z')
if operation == 'save':
    token = request.get('token')
    if not isinstance(token, str) or not pattern.fullmatch(token):
        sys.exit(2)
    fd, temporary = tempfile.mkstemp(prefix='.token-', dir='/auth')
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(token)
            output.flush()
            os.fchmod(output.fileno(), 0o444)
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
elif operation == 'clear':
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
elif operation != 'status':
    sys.exit(2)
if operation in ('save', 'clear'):
    directory = os.open('/auth', os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
authenticated = False
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            sys.exit(2)
        token = source.read(8193)
    authenticated = bool(pattern.fullmatch(token))
except FileNotFoundError:
    pass
print(json.dumps({'authenticated': authenticated}))
'''


LAUNCH_WITH_SHARED_AUTH = r'''import os, re, stat, sys
try:
    fd = os.open('/run/claude-cloud-auth/token', os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError()
        token = source.read(8193)
    if not re.fullmatch(r'sk-ant-oat01-[A-Za-z0-9_-]{64,4096}', token):
        raise ValueError()
except (OSError, ValueError, UnicodeError):
    print('Shared Claude authorization is unavailable; ask the administrator to sign in.', file=sys.stderr)
    sys.exit(78)
environment = dict(os.environ)
for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK',
            'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
            'CLAUDE_CODE_OAUTH_REFRESH_TOKEN', 'CLAUDE_CODE_OAUTH_SCOPES'):
    environment.pop(key, None)
environment['CLAUDE_CODE_OAUTH_TOKEN'] = token
os.execve('/usr/local/bin/claude', ['/usr/local/bin/claude', *sys.argv[1:]], environment)
'''
