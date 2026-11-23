"""Minimal ephemeral X desktop. Browser sandbox is mandatory; never fall back."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

READY = Path('/tmp/login-ready')
children = []
stopping = False


def stop(signum=None, frame=None):
    global stopping
    stopping = True


def launch(args):
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    children.append(proc)
    return proc


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    os.umask(0o077)
    Path(os.environ['HOME']).mkdir(mode=0o700, exist_ok=True)
    profile = Path('/tmp/chrome/Default')
    profile.mkdir(mode=0o700, parents=True)
    (profile / 'Preferences').write_text(json.dumps({
        'webrtc': {'ip_handling_policy': 'disable_non_proxied_udp'},
        'credentials_enable_service': False,
        'autofill': {'credit_card_enabled': False, 'profile_enabled': False},
        'profile': {'password_manager_enabled': False,
                    'default_content_setting_values': {'geolocation': 2, 'notifications': 2}},
    }))
    launch(['Xvfb', ':99', '-screen', '0', '1280x800x24', '-nolisten', 'tcp', '-ac'])
    deadline = time.monotonic() + 10
    while not Path('/tmp/.X11-unix/X99').exists():
        if any(child.poll() is not None for child in children) or time.monotonic() > deadline:
            raise RuntimeError('display startup failed')
        time.sleep(.1)
    launch(['openbox', '--config-file', '/opt/login-browser/openbox.xml'])
    launch(['chromium', '--disable-setuid-sandbox', '--user-data-dir=/tmp/chrome',
            '--proxy-server=http://egress:3128', '--proxy-bypass-list=<-loopback>',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE egress',
            '--disable-quic', '--disable-gpu', '--disable-extensions', '--disable-sync', '--disable-background-networking',
            '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
            '--webrtc-ip-handling-policy=disable_non_proxied_udp',
            '--no-first-run', '--no-default-browser-check', '--password-store=basic',
            '--window-position=0,0', '--window-size=1280,800', '--start-maximized', 'about:blank'])
    launch(['x11vnc', '-display', ':99', '-localhost', '-rfbport', '5900', '-forever',
            '-shared', '-nopw', '-noxdamage', '-quiet', '-no6'])
    time.sleep(1)
    deadline = time.monotonic() + 15
    while True:
        if any(child.poll() is not None for child in children):
            raise RuntimeError('browser startup failed; user namespace sandbox support is required')
        try:
            with socket.create_connection(('127.0.0.1', 5900), timeout=.5) as stream:
                if stream.recv(12).startswith(b'RFB ') and Path('/tmp/chrome/SingletonSocket').exists():
                    break
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError('display relay startup timed out')
        time.sleep(.1)
    READY.write_text(json.dumps({'pids': [child.pid for child in children]}))
    expires = time.monotonic() + max(1, min(int(os.getenv('LOGIN_BROWSER_LIFETIME', '1800')), 1800))
    while not stopping and time.monotonic() < expires:
        if any(child.poll() is not None for child in children):
            raise RuntimeError('login browser stopped')
        time.sleep(.25)


try:
    main()
except Exception:
    # Don't print browser URLs, page contents, cookies or credentials.
    print('Login browser failed. Check resources and unprivileged user namespace sandbox support.', flush=True)
    raise SystemExit(1)
finally:
    READY.unlink(missing_ok=True)
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    for child in reversed(children):
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
