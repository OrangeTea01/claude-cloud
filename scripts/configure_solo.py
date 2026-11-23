"""Configure a fresh personal installation; never replace an existing .env."""
import argparse
import getpass
import os
from pathlib import Path
import sys
import warnings

if __package__:
    from .configure import password_hash
    from .configure_multiuser import PROFILES
    from .enable_https import ConfigError, public_target
else:
    from configure import password_hash
    from configure_multiuser import PROFILES
    from enable_https import ConfigError, public_target


def automatic_profile(meminfo=None):
    try:
        text = meminfo if meminfo is not None else Path('/proc/meminfo').read_text()
        line = next(line for line in text.splitlines() if line.startswith('MemTotal:'))
        kib = int(line.split()[1])
    except (OSError, ValueError, StopIteration, IndexError):
        raise ConfigError('Cannot detect RAM. Specify --profile 8g, 12g, or 24g.') from None
    if kib < 6 * 1024 * 1024:
        raise ConfigError('This deployment profile requires a server with at least 8 GB RAM.')
    return '24g' if kib >= 22 * 1024 * 1024 else '12g' if kib >= 11 * 1024 * 1024 else '8g'


def configure_solo(path, address, password, profile='auto'):
    target = Path(path)
    if os.path.lexists(target):
        raise ConfigError('Existing .env found. No password, mode or data was changed.')
    host, origin = public_target(address)
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ConfigError('Use a website password between 12 and 1024 characters.')
    if profile == 'auto':
        profile = automatic_profile()
    if profile not in PROFILES:
        raise ConfigError('Choose auto, 8g, 12g, or 24g.')
    values = {
        'APP_MODE': 'solo',
        'ADMIN_PASSWORD_HASH': "'" + password_hash(password) + "'",
        'MANAGEMENT_PASSWORD_HASH': '',
        'PUBLIC_ORIGIN': origin, 'COOKIE_SECURE': 'true',
        'DOMAIN': host, 'COMPOSE_PROFILES': 'https',
        'WORKER_IMAGE': 'claude-cloud-worker:local', 'CLAUDE_VERSION': 'stable',
        **PROFILES[profile],
    }
    content = '# Personal installation. Keep this file private.\n'
    content += '\n'.join(f'{key}={value}' for key, value in values.items()) + '\n'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return origin, profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', help='Public IP or domain, without https://')
    parser.add_argument('--profile', choices=['auto', *PROFILES], default='auto')
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / '.env'
    if os.path.lexists(target):
        raise ConfigError('Existing .env found. Nothing was changed.')
    address = args.address or input('Public IP or domain: ').strip()
    public_target(address)
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        password = getpass.getpass('Website password (12-1024 characters): ')
        if password != getpass.getpass('Repeat website password: '):
            raise ConfigError('Passwords do not match. Nothing was written.')
    origin, profile = configure_solo(target, address, password, args.profile)
    print(f'Created personal configuration ({profile}): {origin}')
    print('After deployment, sign in with this password. No username or separate administrator password is needed.')


if __name__ == '__main__':
    try:
        main()
    except (ConfigError, getpass.GetPassWarning) as exc:
        sys.exit(str(exc))
    except (KeyboardInterrupt, EOFError):
        sys.exit('Cancelled. No new configuration was completed.')
