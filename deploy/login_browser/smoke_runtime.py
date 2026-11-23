"""Run on Linux with Docker: verify the actual sandboxed browser without internet or OAuth."""
import argparse
from pathlib import Path
import subprocess
import time
import uuid


def docker(*args, check=True):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=35)
    if check and result.returncode:
        raise RuntimeError('Docker browser smoke failed: ' + result.stderr[-1600:])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='claude-cloud-login-browser:local')
    args = parser.parse_args()
    profile = Path(__file__).with_name('seccomp.json').resolve()
    name = 'cc-login-smoke-' + uuid.uuid4().hex
    created = False
    try:
        docker('create', '--name', name, '--network', 'none', '--init', '--user', '1000:1000',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true',
               '--security-opt', 'seccomp=' + str(profile), '--pids-limit', '256', '--memory', '768m',
               '--memory-swap', '768m', '--cpus', '1', '--shm-size', '128m',
               '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m,mode=1777', args.image)
        created = True
        docker('start', name)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = docker('exec', name, 'python3', '-I', '/opt/login-browser/control.py', 'ready', check=False)
            if result.returncode == 0:
                print('PASS actual Chromium user-namespace sandbox, X display and local-only VNC; network=none, no OAuth.')
                return
            running = docker('inspect', '--format', '{{.State.Running}}', name).stdout.strip()
            if running != 'true':
                raise RuntimeError('Browser startup failed. Verify unprivileged user namespaces and sandbox restrictions on this host.')
            time.sleep(.5)
        raise RuntimeError('Timed out waiting for the sandboxed browser and VNC.')
    finally:
        if created:
            docker('rm', '--force', name, check=False)


if __name__ == '__main__':
    main()
