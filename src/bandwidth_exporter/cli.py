"""Command line: `serve` (the exporter), `run --once` (textfile mode), `check-config`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import tempfile
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

from prometheus_client import CollectorRegistry, generate_latest

from . import __version__, cgroup
from .budget import Budget
from .collector import BandwidthCollector
from .config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    EastWestPlan,
    Settings,
    east_west_plans,
    enabled_tests,
    load_settings,
)
from .control import KeyStore, agent_key
from .engines import iperf3
from .peers import Directory
from .responder import Exclusive, Responder
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


def build_scheduler(settings: Settings, exclusive: Exclusive | None = None) -> Scheduler:
    budget = Budget(limit=settings.budget.limit, reset_day=settings.budget.reset_day)
    scheduler = Scheduler(
        enabled_tests(settings),
        SubprocessRunner(),
        budget=budget,
        state_path=state_path(settings.state_dir),
        startup_delay=settings.startup_delay,
        startup_jitter=settings.startup_jitter,
        trigger_min_interval=settings.trigger.min_interval,
        hours=settings.hours,
        exclusive=exclusive,
    )
    scheduler.restore(load_state(state_path(settings.state_dir)))
    return scheduler


def collector_factory(
    settings: Settings, scheduler: Scheduler, responder: Responder | None = None
) -> partial[BandwidthCollector]:
    peer_role = bool(settings.east_west) or settings.responder.enabled
    return partial(
        BandwidthCollector,
        lambda: scheduler.snapshot,
        network_mode=settings.network_mode,
        revision=os.environ.get("BWEXP_REVISION", ""),
        iperf3_version=iperf3.version(),
        cpu_quota_cores=cgroup.cpu_quota_cores(),
        responder=(responder.stats if responder is not None else None),
        peer_id=settings.own_peer_id if peer_role else "",
    )


def _peer_key(env: str) -> bytes | None:
    try:
        return agent_key(os.environ.get(env), env)
    except ValueError:
        return None


def check_secrets(settings: Settings) -> list[str]:
    """What is missing before this configuration can run."""
    problems = []
    for plan in east_west_plans(settings):
        env = plan.template.key_env
        try:
            agent_key(os.environ.get(env), env)
        except ValueError:
            problems.append(f"{plan.name}: ${env} is empty or shorter than 16 characters")
    if settings.responder.enabled:
        env = settings.responder.keys_env
        try:
            KeyStore.parse(os.environ.get(env, ""))
        except ValueError:
            problems.append(
                f"responder: ${env} must hold one key, or a JSON object of peer id to key, "
                "each at least 16 characters"
            )
    return problems


async def discover(directory: Directory, scheduler: Scheduler, plans: list[EastWestPlan]) -> None:
    """Keep each east/west test's peers current."""
    while True:
        try:
            selected = await directory.refresh()
            for plan in plans:
                peers = selected.get(plan.name, [])
                scheduler.set_peers(plan, [(peer.id, peer.address) for peer in peers])
                if not peers:
                    log.warning("%s: no peers to test right now", plan.name)
        except Exception:
            log.exception("peer discovery failed; keeping the current peers")
        await asyncio.sleep(directory.interval() or 300.0)


def _quiet_server(uvicorn: Any) -> type:
    class Server(uvicorn.Server):  # type: ignore[misc, name-defined]
        """Leaves signals to `_serve`, which stops every server, not just the last one."""

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    return Server


async def _serve(settings: Settings, token: str | None) -> None:
    import uvicorn

    from .app import create_app
    from .control_api import create_control_app

    exclusive = Exclusive()
    scheduler = build_scheduler(settings, exclusive)
    responder = None
    if settings.responder.enabled:
        responder = Responder(
            settings.responder,
            settings.own_peer_id,
            KeyStore.parse(os.environ.get(settings.responder.keys_env, "")),
            exclusive,
            settings.hours,
        )
    plans = east_west_plans(settings)
    if not scheduler.snapshot.tests and not plans and responder is None:
        log.warning("nothing to do: add a test under north_south or east_west")

    metrics_app = create_app(
        settings,
        scheduler,
        collector_factory=collector_factory(settings, scheduler, responder),
        token=token,
        manage_scheduler=False,
    )
    server_class = _quiet_server(uvicorn)
    servers = [
        server_class(
            uvicorn.Config(
                metrics_app,
                host=settings.listen_host,
                port=settings.listen_port,
                access_log=False,
                log_config=None,
                lifespan="on",
            )
        )
    ]
    if responder is not None:
        control_host, control_port = settings.responder.listen.rsplit(":", 1)
        servers.append(
            server_class(
                uvicorn.Config(
                    create_control_app(responder),
                    host=control_host.strip("[]"),
                    port=int(control_port),
                    access_log=False,
                    log_config=None,
                    lifespan="on",
                )
            )
        )

    def stop(*_: object) -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)

    await scheduler.start()
    tasks = []
    if plans:
        directory = Directory(plans, settings.own_peer_id, _peer_key)
        tasks.append(asyncio.create_task(discover(directory, scheduler, plans), name="discovery"))
    log.info(
        "bandwidth-exporter %s as %s: metrics on %s%s; business hours: %s",
        __version__,
        settings.own_peer_id,
        settings.listen,
        f", responder on {settings.responder.listen}" if responder else "",
        settings.hours.describe(),
    )
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await scheduler.stop()


def serve(settings: Settings) -> int:
    from .app import trigger_token

    token = trigger_token(settings)
    if settings.trigger.enabled and token is None:
        log.error(
            "trigger.enabled is true but %s is empty; refusing to start with an open trigger",
            settings.trigger.token_env,
        )
        return 2
    problems = check_secrets(settings)
    if problems:
        for problem in problems:
            log.error("%s", problem)
        return 2
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "state directory %s is not writable (%s); results will not survive a restart",
            settings.state_dir,
            exc,
        )
    asyncio.run(_serve(settings, token))
    return 0


def run_once(settings: Settings, names: list[str], textfile: Path | None) -> int:
    """North/south tests only: east/west needs the long-running process for its peers."""
    scheduler = build_scheduler(settings)
    known = [state.spec.key for state in scheduler.snapshot.tests]
    unknown = [name for name in names if name not in known]
    if unknown:
        log.error("unknown or disabled north/south test(s): %s", ", ".join(unknown))
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


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def _write_atomically(path: Path, payload: bytes) -> None:
    """node_exporter's textfile collector must never read a half-written file."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        # mkstemp creates the file 0600; give it the mode a normal create would (umask
        # applied), so node_exporter, which runs as another user, can read it.
        os.chmod(tmp, 0o666 & ~_umask())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def check_config(settings: Settings) -> int:
    tests = enabled_tests(settings)
    plans = east_west_plans(settings)
    print(f"{settings.own_peer_id}: metrics on {settings.listen}, state in {settings.state_dir}")
    print(f"business hours (no tests): {settings.hours.describe()}")
    budget = settings.budget
    if budget.limit is None:
        print("data budget: unlimited")
    else:
        print(f"data budget: {budget.limit / 1e9:g} GB per period from day {budget.reset_day}")
    if settings.north_south and not settings.runs_north_south:
        print(f"north/south tests run on {settings.north_south_on} only")
    if not tests and not plans:
        print("no enabled tests")
    for spec in tests:
        warmup = "auto" if spec.warmup is None else format_duration(spec.warmup)
        print(
            f"- {spec.name}: {spec.backend} -> {spec.target}; {describe(spec.schedule)}; "
            f"{'+'.join(spec.directions)}, {spec.streams} streams, warm-up {warmup}, "
            f"measure {format_duration(spec.duration)} (cap {format_duration(spec.max_duration)})"
        )
    for plan in plans:
        spec = plan.template
        source = (
            f"DNS {plan.discovery.dns}:{plan.discovery.port}"
            if plan.discovery
            else ", ".join(f"{p.id}={p.address}" for p in plan.peers)
        )
        topology = "every peer" if plan.random_peers is None else f"{plan.random_peers} peers"
        print(
            f"- {spec.name} (east/west, {spec.backend}): {topology} of {source}, at most "
            f"{plan.max_peers}; {describe(spec.schedule)}; {'+'.join(spec.directions)}, "
            f"{spec.streams} streams"
        )
    responder = settings.responder
    if responder.enabled:
        ports = responder.data_ports
        print(
            f"responder on {responder.listen}, data ports {ports.first}-{ports.last}, "
            f"{responder.max_concurrent_tests} slot(s), engines {', '.join(responder.engines)}"
        )
    for problem in check_secrets(settings):
        print(f"! {problem}")
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
    once = commands.add_parser("run", help="run north/south tests once and print the metrics")
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
