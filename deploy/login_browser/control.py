"""Fixed readiness/navigation operations; accepts URL via stdin only."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys


def ready():
    try:
        if not Path('/tmp/chrome/SingletonSocket').exists():
            return False
        data = json.loads(Path('/tmp/login-ready').read_text())
        for pid in data['pids']:
            os.kill(int(pid), 0)
        with socket.create_connection(('127.0.0.1', 5900), timeout=1) as stream:
            return stream.recv(12).startswith(b'RFB ')
    except (OSError, ValueError, KeyError, TypeError):
        return False


if len(sys.argv) != 2 or sys.argv[1] not in ('ready', 'open'):
    raise SystemExit(2)
if not ready():
    raise SystemExit(1)
if sys.argv[1] == 'open':
    payload = sys.stdin.buffer.read(17000)
    if len(payload) >= 17000:
        raise SystemExit(2)
    data = json.loads(payload)
    url = data.get('url')
    if not isinstance(url, str) or not url.startswith('https://') or len(url) > 16384:
        raise SystemExit(2)
    # Root broker has already validated the exact official host/path/query. This
    # process delegates to the existing Chromium instance, never a shell.
    result = subprocess.run(['chromium', '--disable-setuid-sandbox', '--user-data-dir=/tmp/chrome', '--new-tab', url],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
    raise SystemExit(result.returncode)
