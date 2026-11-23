"""Identity-scoped server-side sessions. Credentials never enter browser storage."""
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def valid_password_hash(encoded: str) -> bool:
    return isinstance(encoded, str) and re.fullmatch(r"scrypt\$16384\$8\$1\$[0-9a-f]{32}\$[0-9a-f]{128}", encoded) is not None


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (16384, 8, 1):
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=64)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


@dataclass
class Session:
    token: str
    csrf: str
    expires: float
    user_id: str | None = None
    unlocked_workspaces: dict[str, int] = field(default_factory=dict)


class Sessions:
    def __init__(self, ttl: int = 43200):
        self.ttl = ttl
        self.sessions: dict[str, Session] = {}
        self.attempts: dict[str, list[float]] = {}

    def create(self, user_id: str | None = None) -> Session:
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v.expires > now}
        # One account cannot consume the whole shared session pool by repeatedly
        # signing in. Independent admin Sessions keep their own pool.
        same_user = [token for token, session in self.sessions.items() if session.user_id == user_id]
        if len(same_user) >= 32:
            self.sessions.pop(same_user[0])
        if len(self.sessions) >= 256:
            self.sessions.pop(next(iter(self.sessions)))
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now + self.ttl, user_id)
        self.sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        session = self.sessions.get(token or "")
        if session and session.expires > time.time():
            return session
        self.sessions.pop(token or "", None)
        return None

    def remove(self, token: str | None):
        self.sessions.pop(token or "", None)

    def revoke_all(self):
        self.sessions.clear()

    def revoke_user(self, user_id: str):
        self.sessions = {token: session for token, session in self.sessions.items() if session.user_id != user_id}

    def lock_workspace(self, workspace_id: str):
        for session in self.sessions.values():
            session.unlocked_workspaces.pop(workspace_id, None)

    def allow_login(self, address: str) -> bool:
        now = time.time()
        self.attempts = {k: [t for t in v if now - t < 300] for k, v in self.attempts.items() if v and now - v[-1] < 300}
        # An aggregate bound also covers clients cycling forwarded addresses.
        if sum(map(len, self.attempts.values())) >= 60:
            return False
        recent = self.attempts.setdefault(address, [])
        if len(recent) >= 8:
            return False
        recent.append(now)
        return True
