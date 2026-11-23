"""Durable dashboard credentials; environment value is only the initial seed."""
import os
import re
from pathlib import Path
import sqlite3
import stat
import uuid
from datetime import datetime, timezone

from .auth import valid_password_hash


class InstallationModeError(ValueError):
    """A different account mode must never silently adopt existing data."""


def normalize_username(value):
    if not isinstance(value, str):
        raise ValueError("用户名须为 3–32 个字母、数字、下划线或短横线")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{3,32}", value):
        raise ValueError("用户名须为 3–32 个字母、数字、下划线或短横线")
    return value


class CredentialStore:
    def __init__(self, data_dir, initial_hash, *, app_mode="multiuser"):
        if app_mode not in ("multiuser", "solo"):
            raise InstallationModeError("APP_MODE 必须为 solo 或 multiuser")
        self.app_mode, self.solo_user_id = app_mode, None
        if not valid_password_hash(initial_hash):
            raise ValueError("Invalid dashboard bootstrap hash")
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "auth.sqlite3"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Credential storage must be a regular file")
            os.chmod(path, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            # Schema, bootstrap mode and any legacy adoption are committed as
            # one unit. A failed first startup must not leave a password row
            # which a later startup could mistake for an old installation.
            self.db.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            saved_mode = (self.db.execute("SELECT value FROM credential_meta WHERE key='app_mode'").fetchone()
                          if "credential_meta" in tables else None)
            if saved_mode and saved_mode[0] != app_mode:
                raise InstallationModeError("现有网站的数据模式与 APP_MODE 不一致；请恢复原配置。独享版仅支持全新安装。")
            if app_mode == "solo" and not saved_mode:
                if tables or (directory / "registry.sqlite3").exists():
                    raise InstallationModeError("独享版仅支持全新安装，不能直接接管原多用户数据。请使用新的安装目录和数据卷。")
            old_credentials = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_credentials'").fetchone()
            had_saved_credential = bool(old_credentials and self.db.execute(
                "SELECT 1 FROM dashboard_credentials WHERE id=1").fetchone())
            self.db.execute("CREATE TABLE IF NOT EXISTS dashboard_credentials "
                            "(id INTEGER PRIMARY KEY CHECK (id=1), password_hash TEXT NOT NULL, revision INTEGER NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO dashboard_credentials VALUES (1,?,0)", (initial_hash,))
            self.db.execute("CREATE TABLE IF NOT EXISTS model_policy "
                            "(id INTEGER PRIMARY KEY CHECK (id=1), fable_allowed INTEGER NOT NULL CHECK (fable_allowed IN (0,1)), revision INTEGER NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO model_policy VALUES (1,0,0)")
            self.password_hash, self.revision = self.db.execute(
                "SELECT password_hash,revision FROM dashboard_credentials WHERE id=1").fetchone()
            if not valid_password_hash(self.password_hash) or type(self.revision) is not int or self.revision < 0:
                raise ValueError("Invalid saved dashboard credential")
            allowed, self.policy_revision = self.db.execute("SELECT fable_allowed,revision FROM model_policy WHERE id=1").fetchone()
            if allowed not in (0, 1) or type(self.policy_revision) is not int or self.policy_revision < 0:
                raise ValueError("Invalid saved model policy")
            self.fable_allowed = bool(allowed)
            self.db.execute("CREATE TABLE IF NOT EXISTS dashboard_users "
                            "(id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, "
                            "revision INTEGER NOT NULL, created_at TEXT NOT NULL, "
                            "deleting INTEGER NOT NULL DEFAULT 0 CHECK (deleting IN (0,1)))")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(dashboard_users)")}
            if "deleting" not in columns:
                self.db.execute("ALTER TABLE dashboard_users ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0 "
                                "CHECK (deleting IN (0,1))")
            self.db.execute("CREATE TABLE IF NOT EXISTS credential_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO credential_meta VALUES ('app_mode',?)", (app_mode,))
            legacy = self.db.execute("SELECT value FROM credential_meta WHERE key='legacy_user_id'").fetchone()
            deleted = self.db.execute("SELECT value FROM credential_meta WHERE key='legacy_user_deleted'").fetchone()
            mode = self.db.execute("SELECT value FROM credential_meta WHERE key='user_bootstrap'").fetchone()
            if mode is not None and mode[0] not in ("admin_created", "legacy"):
                raise ValueError("Invalid user bootstrap mode")
            if deleted is not None and deleted[0] != "1":
                raise ValueError("Invalid legacy user deletion marker")
            self.legacy_user_deleted = deleted is not None
            if app_mode == "solo":
                if legacy is not None or deleted is not None:
                    raise InstallationModeError("独享数据包含旧版用户映射，已拒绝启动")
                self.legacy_user_id = None
                solo = self.db.execute("SELECT value FROM credential_meta WHERE key='solo_user_id'").fetchone()
                if solo is None:
                    if saved_mode or self.db.execute("SELECT count(*) FROM dashboard_users").fetchone()[0]:
                        raise InstallationModeError("独享身份记录缺失，已拒绝重新创建账号")
                    self.solo_user_id = uuid.uuid4().hex
                    self.db.execute("INSERT INTO dashboard_users (id,username,password_hash,revision,created_at) VALUES (?,?,?,?,?)",
                                    (self.solo_user_id, "private", self.password_hash, self.revision,
                                     datetime.now(timezone.utc).isoformat()))
                    self.db.execute("INSERT INTO credential_meta VALUES ('solo_user_id',?)", (self.solo_user_id,))
                else:
                    self.solo_user_id = solo[0]
                user = self.user(self.solo_user_id)
                if (not user or user["deleting"] or user["passwordHash"] != self.password_hash
                        or user["revision"] != self.revision
                        or self.db.execute("SELECT count(*) FROM dashboard_users").fetchone()[0] != 1):
                    raise InstallationModeError("独享身份记录不完整，已拒绝启动")
            elif mode is not None and mode[0] == "admin_created":
                if legacy is not None or deleted is not None:
                    raise ValueError("Unexpected legacy user mapping")
                self.legacy_user_id = None
            elif legacy is None:
                # Existing installations keep their durable, web-managed password,
                # never the potentially stale environment bootstrap credential.
                if self.legacy_user_deleted or self.db.execute("SELECT count(*) FROM dashboard_users").fetchone()[0]:
                    raise ValueError("Missing legacy user mapping")
                if mode is not None:
                    raise ValueError("Missing legacy user mapping")
                self.legacy_user_id = None
                if had_saved_credential:
                    self.legacy_user_id = uuid.uuid4().hex
                    self.db.execute("INSERT INTO dashboard_users (id,username,password_hash,revision,created_at) VALUES (?,?,?,?,?)",
                                    (self.legacy_user_id, "owner", self.password_hash, self.revision,
                                     datetime.now(timezone.utc).isoformat()))
                    self.db.execute("INSERT INTO credential_meta VALUES ('legacy_user_id',?)", (self.legacy_user_id,))
            else:
                self.legacy_user_id = legacy[0]
                user = self.user(self.legacy_user_id)
                if self.legacy_user_deleted:
                    if user is not None:
                        raise ValueError("Deleted legacy user still exists")
                elif not user or user["passwordHash"] != self.password_hash or user["revision"] != self.revision:
                    raise ValueError("Invalid legacy user credential")
            if mode is None:
                self.db.execute("INSERT INTO credential_meta VALUES ('user_bootstrap',?)",
                                ("legacy" if self.legacy_user_id is not None else "admin_created",))
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def snapshot(self):
        return self.password_hash, self.revision

    def replace(self, password_hash):
        self.replace_user_password(self.legacy_user_id, password_hash)

    @staticmethod
    def public_user(user):
        return {key: user[key] for key in ("id", "username", "createdAt", "deleting")}

    def _user(self, row):
        if row is None:
            return None
        if not valid_password_hash(row[2]) or type(row[3]) is not int or row[3] < 0:
            raise ValueError("Invalid saved user credential")
        if type(row[5]) is not int or row[5] not in (0, 1):
            raise ValueError("Invalid saved user deletion state")
        return {"id": row[0], "username": row[1], "passwordHash": row[2], "revision": row[3],
                "createdAt": row[4], "deleting": bool(row[5])}

    def user(self, user_id):
        return self._user(self.db.execute("SELECT id,username,password_hash,revision,created_at,deleting "
                                          "FROM dashboard_users WHERE id=?", (user_id,)).fetchone())

    def user_by_username(self, username):
        username = normalize_username(username)
        return self._user(self.db.execute("SELECT id,username,password_hash,revision,created_at,deleting "
                                          "FROM dashboard_users WHERE username=?", (username,)).fetchone())

    def users(self):
        return [self.public_user(self._user(row)) for row in self.db.execute(
            "SELECT id,username,password_hash,revision,created_at,deleting FROM dashboard_users ORDER BY created_at,id")]

    def create_user(self, username, password_hash):
        if self.app_mode == "solo":
            raise ValueError("独享版不创建其他用户")
        username = normalize_username(username)
        if not valid_password_hash(password_hash):
            raise ValueError("Invalid user password hash")
        result = {"id": uuid.uuid4().hex, "username": username,
                  "createdAt": datetime.now(timezone.utc).isoformat(), "deleting": False}
        try:
            self.db.execute("INSERT INTO dashboard_users (id,username,password_hash,revision,created_at) VALUES (?,?,?,?,?)",
                            (result["id"], username, password_hash, 0, result["createdAt"]))
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.rollback()
            raise ValueError("用户名已存在") from None
        except Exception:
            self.db.rollback()
            raise
        return result

    def replace_user_password(self, user_id, password_hash):
        if not valid_password_hash(password_hash):
            raise ValueError("Invalid user password hash")
        user = self.user(user_id)
        if user is None:
            raise ValueError("用户不存在")
        if user["deleting"]:
            raise ValueError("账号正在删除，请先完成清理")
        revision = user["revision"] + 1
        try:
            cursor = self.db.execute("UPDATE dashboard_users SET password_hash=?,revision=? WHERE id=?",
                                     (password_hash, revision, user_id))
            if cursor.rowcount != 1:
                raise ValueError("用户不存在")
            if user_id in (self.legacy_user_id, self.solo_user_id):
                cursor = self.db.execute("UPDATE dashboard_credentials SET password_hash=?,revision=? WHERE id=1",
                                         (password_hash, revision))
                if cursor.rowcount != 1:
                    raise ValueError("Missing dashboard credential")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        # A failure cannot advertise a password that was not durably committed.
        if user_id in (self.legacy_user_id, self.solo_user_id):
            self.password_hash, self.revision = password_hash, revision
        return self.public_user(user)

    def begin_user_deletion(self, user_id):
        return self.begin_user_deletions([user_id])[0]

    def begin_user_deletions(self, user_ids):
        """Persist denial before any asynchronous resource cleanup can start."""
        if self.app_mode == "solo":
            raise ValueError("独享版不删除网站身份")
        if not isinstance(user_ids, (list, tuple)) or not 1 <= len(user_ids) <= 100:
            raise ValueError("请选择 1–100 个用户")
        if any(not isinstance(user_id, str) for user_id in user_ids) or len(set(user_ids)) != len(user_ids):
            raise ValueError("用户列表包含无效或重复的账号")
        users = [self.user(user_id) for user_id in user_ids]
        if any(user is None for user in users):
            raise ValueError("用户不存在")
        pending = [user for user in users if not user["deleting"]]
        if not pending:
            return [self.public_user(user) for user in users]
        legacy_revision = None
        try:
            for user in pending:
                revision = user["revision"] + 1
                cursor = self.db.execute("UPDATE dashboard_users SET deleting=1,revision=? WHERE id=?",
                                         (revision, user["id"]))
                if cursor.rowcount != 1:
                    raise ValueError("用户不存在")
                if user["id"] == self.legacy_user_id:
                    cursor = self.db.execute("UPDATE dashboard_credentials SET revision=? WHERE id=1", (revision,))
                    if cursor.rowcount != 1:
                        raise ValueError("Missing dashboard credential")
                    legacy_revision = revision
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if legacy_revision is not None:
            self.revision = legacy_revision
        return [self.public_user({**user, "deleting": True}) for user in users]

    def delete_user(self, user_id):
        """Finish only an already blocked account after its resources are gone."""
        user = self.user(user_id)
        if user is None:
            return False
        if not user["deleting"]:
            raise ValueError("必须先禁用账号并完成工作区清理")
        try:
            cursor = self.db.execute("DELETE FROM dashboard_users WHERE id=? AND deleting=1", (user_id,))
            if cursor.rowcount != 1:
                raise ValueError("用户删除状态已改变")
            if user_id == self.legacy_user_id:
                # Keep the original mapping and mark completion atomically: an
                # empty user table must never cause bootstrap owner recreation.
                self.db.execute("INSERT OR REPLACE INTO credential_meta VALUES ('legacy_user_deleted','1')")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if user_id == self.legacy_user_id:
            self.legacy_user_deleted = True
        return True

    def close(self):
        self.db.close()

    def model_policy(self):
        return {"fableAllowed": self.fable_allowed, "revision": self.policy_revision}

    def replace_model_policy(self, allowed):
        if type(allowed) is not bool:
            raise ValueError("Invalid model policy")
        if allowed == self.fable_allowed:
            return
        revision = self.policy_revision + 1
        try:
            cursor = self.db.execute("UPDATE model_policy SET fable_allowed=?,revision=? WHERE id=1",
                                     (int(allowed), revision))
            if cursor.rowcount != 1:
                raise ValueError("Missing model policy")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.fable_allowed, self.policy_revision = allowed, revision
