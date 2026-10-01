"""The worker process: runs one test's data plane and prints the result as JSON.

Test traffic never touches the uvicorn event loop that serves `/metrics` and the health probes:
one asyncio process cannot use more than one core, and a test would starve the scrape. The
parent passes the resolved test on stdin, reads one JSON document from stdout, forwards stderr
to its log, and kills the process at the run's hard deadline.

    python -m bandwidth_exporter.worker < spec.json
"""

from __future__ import annotations

import json
import logging
import sys
import traceback

from .engines import get_engine
from .model import RunResult


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        spec = json.load(sys.stdin)
        result = get_engine(spec["backend"])(spec)
    except Exception as exc:  # noqa: BLE001 - every failure must still produce a JSON result
        traceback.print_exc(file=sys.stderr)
        result = RunResult.failure("tool_error", f"worker crashed: {exc!r}"[:500])
    json.dump(result.to_dict(), sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
