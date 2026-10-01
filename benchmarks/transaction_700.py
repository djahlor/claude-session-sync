"""Local durability benchmark approximating the current 696-copy migration."""

import hashlib
import json
import tempfile
import time
from pathlib import Path

from claude_session_sync.model import Operation, Plan
from claude_session_sync.transaction import TransactionEngine


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sources = root / "sources"
        destinations = root / "destinations"
        sources.mkdir()
        destinations.mkdir()
        operations = []
        for index in range(700):
            session_id = "benchmark-{:04d}".format(index)
            content = json.dumps(
                {"sessionId": session_id, "payload": "x" * 6_000}
            ).encode()
            source = sources / "local_{}.json".format(session_id)
            source.write_bytes(content)
            operations.append(
                Operation(
                    kind="create",
                    session_id=session_id,
                    source=source,
                    destination=destinations / source.name,
                    source_digest=hashlib.sha256(content).hexdigest(),
                    destination_digest_or_none=None,
                    size=len(content),
                )
            )
        plan = Plan(
            operations=tuple(operations),
            invalid_replicas=(),
            plan_id="benchmark-700",
            total_bytes=sum(operation.size for operation in operations),
        )
        probes = 0

        def process_probe() -> bool:
            nonlocal probes
            probes += 1
            return False

        started = time.monotonic()
        receipt = TransactionEngine(root / "state", process_probe=process_probe).apply(
            plan
        )
        elapsed = time.monotonic() - started
        print(
            json.dumps(
                {
                    "bytes": receipt.bytes_copied,
                    "duration_ms": round(elapsed * 1_000),
                    "operations": receipt.operation_count,
                    "process_probes": probes,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
