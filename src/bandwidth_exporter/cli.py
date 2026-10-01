"""Command line: `serve` (the exporter), `run --once` (textfile mode), `check-config`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from functools import partial
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest

from . import __version__, cgroup
from .budget import Budget
from .collector import BandwidthCollector
from .config import DEFAULT_CONFIG_PATH, ConfigError, Settings, enabled_tests, load_settings
from .engines import iperf3
from .runner import SubprocessRunner
from .scheduler import Scheduler
from .schedules import describe
from .state import load as load_state
from .state import state_path
from .units import format_duration

log = logging.getLogger("bandwidth_exporter")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        document = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            document["exception"] = self.formatException(record.exc_info)
        return json.dumps(document)


def setup_logging(level: str, fmt: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def _config_path(value: str | None) -> Path | None:
    if value:
        return Path(value)
    return DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.is_file() else None


def build_scheduler(settings: Settings) -> Scheduler:
    budget = Budget(
        limit=settings.budget.limit,
        reset_day=settings.budget.reset_day,
        on_exhausted=settings.budget.on_exhausted,
    )
    scheduler = Scheduler(
        enabled_tests(settings),
        SubprocessRunner(),
        budget=budget,
        state_path=state_path(settings.state_dir),
        startup_delay=settings.startup_delay,
        trigger_min_interval=settings.trigger.min_interval,
    )
    scheduler.restore(load_state(state_path(settings.state_dir)))
    return scheduler


def collector_factory(settings: Settings, scheduler: Scheduler) -> partial[BandwidthCollector]:
    return partial(
        BandwidthCollector,
        lambda: scheduler.snapshot,
        network_mode=settings.network_mode,
        revision=os.environ.get("BWEXP_REVISION", ""),
        iperf3_version=iperf3.version(),
        cpu_quota_cores=cgroup.cpu_quota_cores(),
    )


def serve(settings: Settings) -> int:
    import uvicorn

    from .app import create_app, trigger_token

    token = trigger_token(settings)
    if settings.trigger.enabled and token is None:
        log.error(
            "trigger.enabled is true but %s is empty; refusing to start with an open trigger",
            settings.trigger.token_env,
        )
        return 2
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "state directory %s is not writable (%s); results will not survive a restart",
            settings.state_dir,
            exc,
        )
    scheduler = build_scheduler(settings)
    if not scheduler.snapshot.tests:
        log.warning("no enabled tests: add one under north_south (see config.example.yaml)")
    app = create_app(
        settings,
        scheduler,
        collector_factory=collector_factory(settings, scheduler),
        token=token,
    )
    log.info("bandwidth-exporter %s listening on %s", __version__, settings.listen)
    uvicorn.run(
        app,
        host=settings.listen_host,
        port=settings.listen_port,
        workers=1,  # a second worker would start a second scheduler
        access_log=False,
        log_config=None,
        lifespan="on",
    )
    return 0


def run_once(settings: Settings, names: list[str], textfile: Path | None) -> int:
    scheduler = build_scheduler(settings)
    known = [state.spec.name for state in scheduler.snapshot.tests]
    unknown = [name for name in names if name not in known]
    if unknown:
        log.error("unknown or disabled test(s): %s", ", ".join(unknown))
        return 2
    results = asyncio.run(scheduler.run_once(names or None))
    registry = CollectorRegistry(auto_describe=False)
    registry.register(collector_factory(settings, scheduler)())
    payload = generate_latest(registry)
    if textfile is None:
        sys.stdout.buffer.write(payload)
    else:
        _write_atomically(textfile, payload)
    return 1 if any(result.status == "failure" for result in results) else 0


def _write_atomically(path: Path, payload: bytes) -> None:
    """node_exporter's textfile collector must never read a half-written file."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def check_config(settings: Settings) -> int:
    tests = enabled_tests(settings)
    print(f"listen {settings.listen}, state in {settings.state_dir}")
    budget = settings.budget
    if budget.limit is None:
        print("data budget: unlimited")
    else:
        print(
            f"data budget: {budget.limit / 1e9:g} GB per period from day {budget.reset_day}, "
            f"then {budget.on_exhausted}"
        )
    if not tests:
        print("no enabled tests")
    for spec in tests:
        warmup = "auto" if spec.warmup is None else format_duration(spec.warmup)
        print(
            f"- {spec.name}: {spec.backend} -> {spec.target}; {describe(spec.schedule)}; "
            f"{'+'.join(spec.directions)}, {spec.streams} streams, warm-up {warmup}, "
            f"measure {format_duration(spec.duration)} (cap {format_duration(spec.max_duration)})"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bandwidth-exporter", description=__doc__)
    parser.add_argument(
        "--config",
        default=os.environ.get("BWEXP_CONFIG"),
        help=f"YAML configuration file (default: $BWEXP_CONFIG or {DEFAULT_CONFIG_PATH})",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the exporter (default)")
    once = commands.add_parser("run", help="run tests once and print or write the metrics")
    once.add_argument("--once", action="store_true", required=True, help="required: run once")
    once.add_argument("--test", action="append", default=[], help="test name (repeatable)")
    once.add_argument("--textfile", type=Path, help="write a .prom file for node_exporter")
    commands.add_parser("check-config", help="validate the configuration and print the plan")
    commands.add_parser("version", help="print the version")
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0
    try:
        settings = load_settings(_config_path(args.config))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    setup_logging(settings.log_level, settings.log_format)
    if args.command == "check-config":
        return check_config(settings)
    if args.command == "run":
        return run_once(settings, args.test, args.textfile)
    return serve(settings)


if __name__ == "__main__":
    sys.exit(main())
