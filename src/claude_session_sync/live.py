"""Sync while Claude is open and signed out, so a switch needs no restart for it.

Claude Desktop reads an account's chat folder and routine list when that
account signs in, and does not watch them while it runs (4.4212.0). While it
is signed out it still saves the chats of the account just left into that
account's folder, and holds no other account's folder. So this pass writes
chats, archive state and routines into every folder except the account just
left's, and the engine checks right before writing that Claude is still
signed out. The next sign-in reads what was written.

Groups and browser logins live in stores Claude locks while it runs, so they
still need Claude closed. The result names every reason the switch still needs
a restart. When groups are the last reason, a check after the sign-in can still
skip it: Claude loads the new account's groups from Anthropic's servers, and
when they already match the main account's, a restart would copy nothing.
"""

from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from .filesystem import normalized_path
from .layout_records import NOT_RECONCILED
from .logins import last_known_account, signed_in

# Claude retires sessions parked for other accounts at sign-out. Their last
# saves land within a second, so the pass waits this long before it plans.
SIGNED_OUT_SETTLE_SECONDS = 1.0

# Claude's web view merges the new account's groups a few seconds after the
# sign-in, and Chromium saves them to disk a few seconds after that.
GROUPS_CHECK_SECONDS = 20.0
GROUPS_CHECK_INTERVAL = 1.0
# Chromium writes Local Storage to disk in delayed batches. Same groups are read
# once more a little later, so a copy taken between two writes cannot pass alone.
GROUPS_CONFIRM_SECONDS = 6.0

# What the groups check found, the signed-in scope and the main scope.
GroupsFound = tuple[str, Optional[str], Optional[str]]


def signed_in_again(data_root: Path, departing: str) -> bool:
    """True unless Claude is known to be still signed out of departing.

    lastKnownAccountUuid changes at a sign-in to another account. The flag
    also covers a sign-in back to the same one.
    """

    return (
        signed_in(data_root) is not False or last_known_account(data_root) != departing
    )


def holds_folders(processes: Iterable[Any], data_root: Path, departing: str) -> bool:
    """True while a Claude process may hold a folder other than departing's.

    A Claude of another profile holds its own folders whatever this one does.
    """

    processes = tuple(processes)
    if not processes:
        return False
    root = normalized_path(data_root)
    if any(normalized_path(process.user_data_dir) != root for process in processes):
        return True
    return signed_in_again(data_root, departing)


class KeepAccountsUnchanged:
    """An engine that never writes into the given accounts' folders.

    The chats it holds back stay planned, so the next sync writes them.
    """

    def __init__(self, engine: Any, accounts: frozenset[str]) -> None:
        self.engine = engine
        self.accounts = accounts
        self.held_back = 0

    def _kept(self, operation: Any) -> bool:
        # A chat file's folder is <data root>/claude-code-sessions/<account>/<workspace>.
        account = Path(operation.destination).parent.parent.name.lower()
        return account not in self.accounts

    def apply(self, plan: Any) -> Any:
        operations = tuple(op for op in plan.operations if self._kept(op))
        self.held_back += len(plan.operations) - len(operations)
        if len(operations) != len(plan.operations):
            plan = replace(
                plan,
                operations=operations,
                total_bytes=sum(op.size for op in operations if op.kind != "retire"),
            )
        return self.engine.apply(plan)

    def close_interrupted_runs(self) -> int:
        return self.engine.close_interrupted_runs()


def restart_reasons(config: Any, payload: dict) -> list[str]:
    """Why the switch still needs Claude closed once. Empty when it does not."""

    reasons = []
    if payload.get("signed_in_during_sync"):
        reasons.append("signed-in-during-sync")
    if payload.get("state") not in ("committed", "noop"):
        reasons.append("chats")
    if payload.get("archive") == "waiting":
        reasons.append("archive")
    routines = payload.get("routines", {})
    if routines.get("state") not in (None, "synced", "noop", "disabled"):
        reasons.append("routines")
    if config.sync_sidebar_layout:
        reasons.append("groups")
    if config.sync_browser_logins:
        reasons.append("browser-logins")
    return reasons


def wait_for_groups(
    check: Callable[[], GroupsFound],
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> Optional[GroupsFound]:
    """Run check until it finds more than NOT_RECONCILED. None at the time limit."""

    deadline = clock() + GROUPS_CHECK_SECONDS
    while True:
        found = check()
        if found[0] != NOT_RECONCILED:
            return found
        if clock() >= deadline:
            return None
        sleeper(GROUPS_CHECK_INTERVAL)
