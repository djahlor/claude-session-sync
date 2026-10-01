"""Strict configuration loading for explicitly selected Claude profiles."""

from . import strict_json as json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Set, Tuple, Union

from .model import Profile


class ConfigError(ValueError):
    """Raised when a configuration is unsafe or does not match the schema."""


@dataclass(frozen=True)
class ApprovedTarget:
    profile_name: str
    account_id: str
    workspace_id: str


@dataclass(frozen=True)
class Config:
    profiles: Tuple[Profile, ...]
    state_dir: Path
    retention: int
    acknowledge_cross_profile_copy: bool
    acknowledge_cross_account_copy: bool
    claude_executable: Path
    approved_targets: Tuple[ApprovedTarget, ...] = ()
    target_policy: str = "approved-only"
    sync_sidebar_layout: bool = False
    sync_code_routines: bool = False
    version: int = 1


_CONFIG_KEYS = {
    "version",
    "profiles",
    "approved_targets",
    "state_dir",
    "retention",
    "acknowledge_cross_profile_copy",
    "acknowledge_cross_account_copy",
    "claude_executable",
    "target_policy",
    "sync_sidebar_layout",
    "sync_code_routines",
}
_REQUIRED_CONFIG_KEYS = _CONFIG_KEYS - {
    "target_policy",
    "sync_sidebar_layout",
    "sync_code_routines",
}
_PROFILE_KEYS = {"name", "data_root", "launch_command", "enabled", "is_default"}
_TARGET_KEYS = {"profile", "account", "workspace"}
_TARGET_POLICIES = {"approved-only", "all-configured-profiles", "logins"}


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("{} must be a JSON object".format(label))
    return value


def _unknown_keys(value: Mapping[str, Any], allowed: Set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError("unknown {} keys: {}".format(label, ", ".join(unknown)))


def _required(value: Mapping[str, Any], keys: Set[str], label: str) -> None:
    missing = sorted(keys - set(value))
    if missing:
        raise ConfigError("missing {} keys: {}".format(label, ", ".join(missing)))


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("{} must be a non-empty string".format(label))
    return value


def _parse_profile(raw: Any, index: int) -> Tuple[Profile, bool]:
    profile = _object(raw, "profile {}".format(index))
    _unknown_keys(profile, _PROFILE_KEYS, "profile")
    _required(
        profile,
        {"name", "data_root", "launch_command", "is_default"},
        "profile",
    )
    name = _nonempty_string(profile["name"], "profile name")
    data_root = Path(
        _nonempty_string(profile["data_root"], "profile data_root")
    ).expanduser()
    raw_command = profile["launch_command"]
    if (
        not isinstance(raw_command, list)
        or not raw_command
        or any(not isinstance(part, str) or not part for part in raw_command)
    ):
        raise ConfigError("profile launch_command must be a non-empty array of strings")
    # Configs from older versions hold a disabled Personal profile until setup
    # removes it, and setup must read them first.
    enabled = profile.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("profile enabled must be a boolean")
    is_default = profile["is_default"]
    if not isinstance(is_default, bool):
        raise ConfigError("profile is_default must be a boolean")
    return Profile(name, data_root, tuple(raw_command), is_default), enabled


def _parse_approved_target(raw: Any, index: int) -> ApprovedTarget:
    target = _object(raw, "approved target {}".format(index))
    _unknown_keys(target, _TARGET_KEYS, "approved target")
    _required(target, _TARGET_KEYS, "approved target")
    return ApprovedTarget(
        _nonempty_string(target["profile"], "approved target profile"),
        _nonempty_string(target["account"], "approved target account"),
        _nonempty_string(target["workspace"], "approved target workspace"),
    )


def load_config(path: Union[str, Path]) -> Config:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(
            "cannot load config {}: {}".format(config_path, error)
        ) from error

    document = _object(raw, "config")
    _unknown_keys(document, _CONFIG_KEYS, "config")
    _required(document, _REQUIRED_CONFIG_KEYS, "config")

    version = document["version"]
    if isinstance(version, bool) or version != 1:
        raise ConfigError("config version must be 1")

    raw_profiles = document["profiles"]
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ConfigError("profiles must be a non-empty array")
    parsed = [
        _parse_profile(profile, index) for index, profile in enumerate(raw_profiles)
    ]
    names = [profile.name for profile, _enabled in parsed]
    if len(names) != len(set(names)):
        raise ConfigError("profile names must be unique")
    profiles = tuple(profile for profile, enabled in parsed if enabled)
    if not profiles:
        raise ConfigError("at least one profile must be enabled")
    default_profiles = [profile.name for profile in profiles if profile.is_default]
    if len(default_profiles) > 1:
        raise ConfigError("at most one enabled profile may be is_default")

    raw_targets = document["approved_targets"]
    if not isinstance(raw_targets, list):
        raise ConfigError("approved_targets must be an array")
    approved_targets = tuple(
        _parse_approved_target(target, index)
        for index, target in enumerate(raw_targets)
    )
    approved_keys = {
        (target.profile_name, target.account_id, target.workspace_id)
        for target in approved_targets
    }
    if len(approved_keys) != len(approved_targets):
        raise ConfigError("approved_targets must be unique")
    known_profile_names = set(names)
    unknown_profiles = sorted(
        {target.profile_name for target in approved_targets} - known_profile_names
    )
    if unknown_profiles:
        raise ConfigError(
            "approved_targets reference unknown profiles: {}".format(
                ", ".join(unknown_profiles)
            )
        )

    state_dir = Path(_nonempty_string(document["state_dir"], "state_dir")).expanduser()
    retention = document["retention"]
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
        raise ConfigError("retention must be a positive integer")
    acknowledgement = document["acknowledge_cross_profile_copy"]
    if not isinstance(acknowledgement, bool):
        raise ConfigError("acknowledge_cross_profile_copy must be a boolean")
    if len(profiles) > 1 and acknowledgement is not True:
        raise ConfigError(
            "acknowledge_cross_profile_copy must be true when multiple profiles are enabled"
        )
    account_acknowledgement = document["acknowledge_cross_account_copy"]
    if not isinstance(account_acknowledgement, bool):
        raise ConfigError("acknowledge_cross_account_copy must be a boolean")
    target_policy = document.get("target_policy", "approved-only")
    if target_policy not in _TARGET_POLICIES:
        raise ConfigError(
            "target_policy must be one of: {}".format(
                ", ".join(sorted(_TARGET_POLICIES))
            )
        )
    if target_policy in ("all-configured-profiles", "logins") and not account_acknowledgement:
        raise ConfigError(
            "acknowledge_cross_account_copy must be true when target_policy "
            "is {}".format(target_policy)
        )

    sync_sidebar_layout = document.get("sync_sidebar_layout", False)
    if not isinstance(sync_sidebar_layout, bool):
        raise ConfigError("sync_sidebar_layout must be a boolean")
    sync_code_routines = document.get("sync_code_routines", False)
    if not isinstance(sync_code_routines, bool):
        raise ConfigError("sync_code_routines must be a boolean")

    claude_executable = Path(
        _nonempty_string(document["claude_executable"], "claude_executable")
    ).expanduser()

    return Config(
        profiles=profiles,
        state_dir=state_dir,
        retention=retention,
        acknowledge_cross_profile_copy=acknowledgement,
        acknowledge_cross_account_copy=account_acknowledgement,
        claude_executable=claude_executable,
        approved_targets=approved_targets,
        target_policy=target_policy,
        sync_sidebar_layout=sync_sidebar_layout,
        sync_code_routines=sync_code_routines,
        version=version,
    )
