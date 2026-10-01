"""Configuration commands, separate from synchronization execution."""

import os
import tempfile
from pathlib import Path

from .config import Config, load_config
from . import strict_json as json


def _approve_current_targets(config_path: Path, config: Config, *, apply: bool) -> dict:
    from .filesystem import atomic_write_bytes, ensure_private_directory
    from .store import SessionStore

    discovery = SessionStore().discover(config)
    if discovery.invalid_replicas or not discovery.targets:
        return {
            "state": "blocked",
            "counts": {
                "approved": len(config.approved_targets),
                "invalid_replicas": len(discovery.invalid_replicas),
                "targets": len(discovery.targets),
            },
        }
    approved = [
        {
            "profile": target.profile_name,
            "account": target.account_id,
            "workspace": target.workspace_id,
        }
        for target in discovery.targets
    ]
    current = {
        (target.profile_name, target.account_id, target.workspace_id)
        for target in config.approved_targets
    }
    discovered = {
        (target.profile_name, target.account_id, target.workspace_id)
        for target in discovery.targets
    }
    if apply:
        document = json.loads(config_path.read_text(encoding="utf-8"))
        document["approved_targets"] = approved
        encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        ensure_private_directory(config_path.parent)
        atomic_write_bytes(config_path, encoded)
        os.chmod(str(config_path), 0o600)
        # Validate the exact durable document before reporting success.
        load_config(config_path)
    return {
        "state": "approved" if apply else "planned",
        "counts": {
            "approved": len(discovered),
            "new": len(discovered - current),
            "removed": len(current - discovered),
            "targets": len(discovered),
        },
    }


def _validate_config_data(encoded: bytes) -> Config:
    descriptor, raw_path = tempfile.mkstemp(prefix="claude-session-sync-config-")
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
        os.chmod(str(path), 0o600)
        return load_config(path)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _prepare_config_data(
    source: bytes,
    *,
    automatic_targets: bool,
    sync_layout: bool,
    sync_routines: bool,
) -> tuple[bytes, dict]:
    """Return validated desired config bytes and a safe change summary."""

    document = json.loads(source.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("configuration document must be an object")
    changes = _apply_config_options(
        document,
        automatic_targets=automatic_targets,
        sync_layout=sync_layout,
        sync_routines=sync_routines,
    )
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _validate_config_data(encoded)
    return encoded, _config_summary(document, changes)


def _configure(
    config_path: Path,
    *,
    automatic_targets: bool,
    sync_layout: bool,
    sync_routines: bool,
    apply: bool,
) -> dict:
    from .filesystem import atomic_write_bytes, ensure_private_directory

    load_config(config_path)
    encoded, payload = _prepare_config_data(
        config_path.read_bytes(),
        automatic_targets=automatic_targets,
        sync_layout=sync_layout,
        sync_routines=sync_routines,
    )

    changes = payload["counts"]["changes"]
    if apply and changes:
        ensure_private_directory(config_path.parent)
        atomic_write_bytes(config_path, encoded)
        os.chmod(str(config_path), 0o600)
        load_config(config_path)
    payload["state"] = (
        "configured" if apply and changes else "planned" if changes else "noop"
    )
    return payload


def _apply_config_options(
    document: dict,
    *,
    automatic_targets: bool,
    sync_layout: bool,
    sync_routines: bool,
) -> int:
    changes = _drop_personal_profile(document)

    if automatic_targets:
        # Sync every folder a real login uses, and let a new login join once
        # Claude writes a chat there. Leftover folders are ignored.
        desired = {
            "target_policy": "logins",
            "acknowledge_cross_account_copy": True,
        }
        for key, value in desired.items():
            if document.get(key) != value:
                document[key] = value
                changes += 1

    if sync_layout and document.get("sync_sidebar_layout") is not True:
        document["sync_sidebar_layout"] = True
        changes += 1

    if sync_routines and document.get("sync_code_routines") is not True:
        document["sync_code_routines"] = True
        changes += 1

    return changes


def _drop_personal_profile(document: dict) -> int:
    """Remove the Personal profile older versions generated, with its targets.

    Runs before validation, so it skips entries that are not objects and
    leaves them for the validator to name.
    """

    def personal(entry, key: str) -> bool:
        return isinstance(entry, dict) and entry.get(key) == "Personal"

    profiles = document.get("profiles")
    if not isinstance(profiles, list) or not any(personal(p, "name") for p in profiles):
        return 0
    document["profiles"] = [profile for profile in profiles if not personal(profile, "name")]
    targets = document.get("approved_targets")
    if isinstance(targets, list):
        document["approved_targets"] = [
            target for target in targets if not personal(target, "profile")
        ]
    return 1


def _config_summary(document: dict, changes: int) -> dict:
    return {
        "state": "planned" if changes else "noop",
        "counts": {
            "changes": changes,
            "automatic_targets": int(
                document.get("target_policy") in ("all-configured-profiles", "logins")
            ),
            "layout_sync": int(document.get("sync_sidebar_layout") is True),
            "routine_sync": int(document.get("sync_code_routines") is True),
        },
    }
