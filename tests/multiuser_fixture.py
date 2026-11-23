"""Explicit ownership for legacy API fakes; production brokers remain strict."""
from unittest.mock import Mock
from pathlib import Path
import sqlite3

from backend.docker_backend import BrokerError


def seed_legacy_credentials(data_dir, initial_hash):
    """Explicitly model an installed, pre-multiuser website in legacy tests.

    Fresh production databases have no user. Historical API tests opt into an
    existing legacy credential so startup can exercise the real migration.
    """
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "auth.sqlite3"
    if path.exists():
        return
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute("CREATE TABLE dashboard_credentials "
                       "(id INTEGER PRIMARY KEY CHECK (id=1), password_hash TEXT NOT NULL, revision INTEGER NOT NULL)")
            db.execute("INSERT INTO dashboard_credentials VALUES (1,?,0)", (initial_hash,))
    finally:
        db.close()


def legacy_app(config, *args, **kwargs):
    """Create the application with a real old single-password credential DB."""
    from backend.app import create_app
    seed_legacy_credentials(config.data_dir, config.password_hash)
    return create_app(config, *args, **kwargs)


def legacy_credential_store(data_dir, initial_hash):
    from backend.credentials import CredentialStore
    seed_legacy_credentials(data_dir, initial_hash)
    return CredentialStore(data_dir, initial_hash)


def attach_ownership(broker, workspace_ids=()):
    """Adopt only registered fake workspaces when application startup migrates."""
    registered = set(workspace_ids)
    records = {}
    legacy_owner = None

    if not hasattr(broker, "get"):
        def get(workspace_id):
            if workspace_id not in registered:
                raise BrokerError("工作区不存在", 404)
            record = records.get(workspace_id, {})
            return {"id": workspace_id, "ownerId": record.get("ownerId", legacy_owner),
                    "passwordProtected": bool(record.get("passwordHash"))}
        broker.get = Mock(side_effect=get)

    def assign_legacy_owner(user_id):
        nonlocal legacy_owner
        legacy_owner = user_id
        for record in records.values():
            if not record["ownerId"]:
                record["ownerId"] = user_id

    def workspace_access(workspace_id):
        # Ask the current fake registry, so unknown-workspace tests retain 404s.
        item = broker.get(workspace_id)
        return dict(records.setdefault(workspace_id, {
            "ownerId": item.get("ownerId") or legacy_owner,
            "passwordHash": None, "passwordRevision": 0,
        }))

    def set_workspace_password(workspace_id, encoded):
        workspace_access(workspace_id)
        record = records[workspace_id]
        record["passwordHash"] = encoded
        record["passwordRevision"] += 1
        return record["passwordRevision"]

    broker.assign_legacy_owner = Mock(side_effect=assign_legacy_owner)
    broker.workspace_access = Mock(side_effect=workspace_access)
    broker.set_workspace_password = Mock(side_effect=set_workspace_password)
    return broker
