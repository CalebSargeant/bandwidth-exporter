"""Configuration: a YAML file for the test list, environment variables for scalars and secrets.

Loading validates everything up front (durations, cron expressions, backend options, names
that become label values) and rejects unknown keys, so a typo fails at start-up instead of
silently changing what gets measured. Secrets never live in the file: the file names the
environment variable that holds them.
"""

from __future__ import annotations

import ipaddress
import random
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from .businesshours import DAYS, BusinessHours, Window, parse_clock
from .schedules import CronSchedule, RandomSchedule, Schedule, next_run
from .units import parse_bytes, parse_duration, parse_rate

DEFAULT_PORT = 10056
DEFAULT_CONTROL_PORT = 10057
DEFAULT_CONFIG_PATH = Path("/etc/bandwidth-exporter/config.yaml")

Duration = Annotated[float, BeforeValidator(parse_duration)]
Size = Annotated[int, BeforeValidator(parse_bytes)]
Rate = Annotated[float, BeforeValidator(parse_rate)]
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Clock = str | int

# Test names and peer ids become label values on every series: short, stable and boring.
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
# Hostnames that may reach a command line (iperf3 -c). No leading dash, no spaces, no shell.
_HOST = re.compile(r"^(?!-)[A-Za-z0-9.-]{1,253}$")
_CCA = re.compile(r"^[a-z0-9_]{1,32}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# Cloudflare answers 403 to `__down?bytes=` of 100 MB and more (observed 2026-10-01).
CLOUDFLARE_MAX_DOWNLOAD_CHUNK = 99_999_999
# The automatic warm-up ends after 5 s at the latest.
AUTO_WARMUP_CAP = 5.0


class ConfigError(ValueError):
    """The configuration cannot be used. The message says what to fix."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_name(value: Any, what: str = "test names") -> str:
    if isinstance(value, bool):
        raise ValueError("quote the name: YAML reads on, off, yes and no as true or false")
    if not isinstance(value, str) or not _NAME.match(value):
        raise ValueError(
            f"{what} are lower-case letters, digits, '.', '_' and '-', up to 63 characters, "
            "starting with a letter or digit"
        )
    return value


def _check_env(value: str) -> str:
    if not _ENV.match(value):
        raise ValueError(f"{value!r} is not an environment variable name")
    return value


class RandomScheduleConfig(_Strict):
    mean: Duration
    min: Duration
    max: Duration


class ScheduleConfig(_Strict):
    """Either `random: {mean, min, max}` or `cron: "<expr>"` with optional jitter."""

    random: RandomScheduleConfig | None = None
    cron: str | None = None
    jitter: Duration = 0.0
    timezone: str = "UTC"

    @model_validator(mode="after")
    def _exactly_one(self) -> ScheduleConfig:
        if (self.random is None) == (self.cron is None):
            raise ValueError("schedule needs exactly one of `random` or `cron`")
        if self.random is not None and (self.jitter or self.timezone != "UTC"):
            raise ValueError("`jitter` and `timezone` only apply to cron schedules")
        if self.cron is not None and len(self.cron.split()) != 5:
            raise ValueError("cron needs five fields (minute hour day month weekday)")
        self.build()
        return self

    def build(self) -> Schedule:
        if self.random is not None:
            return RandomSchedule(self.random.mean, self.random.min, self.random.max)
        return CronSchedule(self.cron or "", self.jitter, self.timezone)


def _default_schedule() -> ScheduleConfig:
    return ScheduleConfig(random=RandomScheduleConfig(mean="4h", min="1h", max="12h"))


Direction = Literal["download", "upload"]


class Defaults(_Strict):
    schedule: ScheduleConfig = Field(default_factory=_default_schedule)
    warmup: Literal["auto"] | Duration = "auto"
    duration: Duration = 10.0
    max_duration: Duration = 15.0
    streams: int = Field(default=4, ge=1, le=32)
    directions: tuple[Direction, ...] = ("download", "upload")
    early_stop: bool = True


class PlanConfig(_Strict):
    download: Rate | None = None
    upload: Rate | None = None


class CloudflareOptions(_Strict):
    base_url: str = "https://speed.cloudflare.com"
    download_chunk: Size = 50_000_000
    upload_chunk: Size = 25_000_000
    latency_samples: int = Field(default=20, ge=3, le=100)
    loaded_latency_interval: Duration = 0.4
    # Plain HTTP exists for tests against a local fake; production must use HTTPS.
    allow_insecure_http: bool = False

    @model_validator(mode="after")
    def _check(self) -> CloudflareOptions:
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")
        if self.base_url.startswith("http://") and not self.allow_insecure_http:
            raise ValueError("base_url must use https (allow_insecure_http is for tests only)")
        if not 1_000_000 <= self.download_chunk <= CLOUDFLARE_MAX_DOWNLOAD_CHUNK:
            raise ValueError("download_chunk must be between 1MB and 99.9MB")
        if not 1_000_000 <= self.upload_chunk <= 100_000_000:
            raise ValueError("upload_chunk must be between 1MB and 100MB")
        if not 0.05 <= self.loaded_latency_interval <= 5:
            raise ValueError("loaded_latency_interval must be between 50ms and 5s")
        return self


class Iperf3Options(_Strict):
    binary: str = "iperf3"
    connect_timeout: Duration = 5.0

    @field_validator("binary")
    @classmethod
    def _binary(cls, value: str) -> str:
        if value.startswith("-") or any(ch.isspace() for ch in value):
            raise ValueError("binary must be a path or command name")
        return value


class BuiltinOptions(_Strict):
    """The built-in raw-TCP engine, east/west only: it needs a bandwidth-exporter responder."""

    latency_samples: int = Field(default=10, ge=3, le=100)
    loaded_latency_interval: Duration = 0.4
    # Bytes per test the agent asks for; the responder's own cap still applies. 20 GB covers
    # a 15 s direction at about 10 Gbit/s.
    max_bytes: Size = 20_000_000_000


BACKEND_OPTIONS: dict[str, type[_Strict]] = {
    "cloudflare": CloudflareOptions,
    "iperf3": Iperf3Options,
    "builtin": BuiltinOptions,
}


class _TestCommon(_Strict):
    name: str
    enabled: bool = True
    schedule: ScheduleConfig | None = None
    warmup: Literal["auto"] | Duration | None = None
    duration: Duration | None = None
    max_duration: Duration | None = None
    streams: int | None = Field(default=None, ge=1, le=32)
    directions: tuple[Direction, ...] | None = None
    early_stop: bool | None = None
    plan: PlanConfig | None = None
    ip_family: Literal["auto", "ipv4", "ipv6"] = "auto"
    bind_address: str | None = None
    congestion_control: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _engine_alias(cls, data: Any) -> Any:
        # `engine` is accepted as a synonym: the design document uses both words.
        if isinstance(data, dict) and "engine" in data:
            if "backend" in data:
                raise ValueError("set `backend` or `engine`, not both")
            data = {**data, "backend": data["engine"]}
            del data["engine"]
        return data

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str:
        return _check_name(value)

    @field_validator("bind_address")
    @classmethod
    def _bind(cls, value: str | None) -> str | None:
        if value is not None:
            ipaddress.ip_address(value)
        return value

    @field_validator("congestion_control")
    @classmethod
    def _cca(cls, value: str | None) -> str | None:
        if value is not None and not _CCA.match(value):
            raise ValueError("congestion_control must be a kernel algorithm name, e.g. bbr")
        return value


class NorthSouthTest(_TestCommon):
    backend: Literal["cloudflare", "iperf3"]
    target: str | None = None

    @model_validator(mode="after")
    def _backend_rules(self) -> NorthSouthTest:
        BACKEND_OPTIONS[self.backend].model_validate(self.options)
        if self.backend == "iperf3":
            if not self.target:
                raise ValueError("iperf3 tests need `target: host:port` (your own iperf3 server)")
            parse_host_port(self.target, default_port=5201)
        elif self.target is not None:
            raise ValueError("cloudflare tests take no `target`; set options.base_url instead")
        return self


class StaticPeer(_Strict):
    """A peer by name and the address of its responder's control API."""

    id: str
    address: str

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, value: Any) -> str:
        return _check_name(value, "peer ids")

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        host, port = parse_host_port(value, default_port=DEFAULT_CONTROL_PORT)
        return format_host_port(host, port)


class DnsDiscovery(_Strict):
    """Every address a DNS name resolves to is a peer: a headless Service returns one record
    per ready pod. Each peer's id comes from its responder, never from the address."""

    dns: str
    port: int = Field(default=DEFAULT_CONTROL_PORT, ge=1, le=65535)
    refresh: Duration = 300.0

    @field_validator("dns")
    @classmethod
    def _dns(cls, value: str) -> str:
        if not _HOST.match(value):
            raise ValueError(f"bad DNS name {value!r}")
        return value


class TopologyConfig(_Strict):
    # Each agent tests this many peers, picked by rendezvous hashing so the pairing survives
    # restarts. Unset means every peer (a full mesh: N x (N-1) pairs).
    random_peers: int | None = Field(default=3, ge=1)


class PeerAuthConfig(_Strict):
    # The key this agent signs its requests with.
    key_env: str = "BWEXP_PEER_KEY"

    @field_validator("key_env")
    @classmethod
    def _key_env(cls, value: str) -> str:
        return _check_env(value)


class EastWestTest(_TestCommon):
    backend: Literal["builtin", "iperf3"] = "builtin"
    peers: tuple[StaticPeer, ...] = ()
    discovery: DnsDiscovery | None = None
    topology: TopologyConfig = Field(default_factory=TopologyConfig)
    max_peers: int = Field(default=10, ge=1, le=1000)
    auth: PeerAuthConfig = Field(default_factory=PeerAuthConfig)

    @model_validator(mode="after")
    def _peers(self) -> EastWestTest:
        BACKEND_OPTIONS[self.backend].model_validate(self.options)
        if bool(self.peers) == (self.discovery is not None):
            raise ValueError("east/west tests need exactly one of `peers` or `discovery`")
        ids = [peer.id for peer in self.peers]
        if len(ids) != len(set(ids)):
            raise ValueError("peer ids must be unique within a test")
        return self


class DataPorts(_Strict):
    first: int = 5201
    last: int = 5210

    @model_validator(mode="after")
    def _range(self) -> DataPorts:
        # Unprivileged ports only: the container drops every capability.
        if not 1024 <= self.first <= self.last <= 65535:
            raise ValueError("data_ports must be a range within 1024-65535")
        return self


class ResponderConfig(_Strict):
    enabled: bool = False
    listen: str = f"0.0.0.0:{DEFAULT_CONTROL_PORT}"
    data_ports: DataPorts = Field(default_factory=DataPorts)
    max_concurrent_tests: int = Field(default=1, ge=1, le=16)
    # Longest slot a peer may hold, warm-up included.
    max_duration: Duration = 30.0
    max_bytes_per_test: Size = 20_000_000_000
    # Peer ids allowed to test against this responder; empty allows any peer with a valid key.
    allowed_peers: tuple[str, ...] = ()
    # Either one key every peer uses, or a JSON object of peer id to key.
    keys_env: str = "BWEXP_PEER_KEYS"
    respect_business_hours: bool = True
    engines: tuple[Literal["builtin", "iperf3"], ...] = ("builtin", "iperf3")

    @field_validator("listen")
    @classmethod
    def _listen(cls, value: str) -> str:
        parse_host_port(value, default_port=None)
        return value

    @field_validator("keys_env")
    @classmethod
    def _keys_env(cls, value: str) -> str:
        return _check_env(value)

    @field_validator("allowed_peers", mode="before")
    @classmethod
    def _allowed(cls, value: Any) -> Any:
        for item in value or ():
            _check_name(item, "peer ids")
        return value


class BusinessHoursWindowConfig(_Strict):
    days: tuple[Day, ...] = ("mon", "tue", "wed", "thu", "fri")
    start: Clock
    end: Clock

    def build(self) -> Window:
        return Window(
            days=frozenset(DAYS.index(day) for day in self.days),
            start=parse_clock(self.start),
            end=parse_clock(self.end),
        )


class BusinessHoursConfig(_Strict):
    """Hours in which no throughput test starts. `days`/`start`/`end` describe one window;
    `windows` lists more."""

    timezone: str = "UTC"
    days: tuple[Day, ...] | None = None
    start: Clock | None = None
    end: Clock | None = None
    windows: tuple[BusinessHoursWindowConfig, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> BusinessHoursConfig:
        if (self.start is None) != (self.end is None):
            raise ValueError("business hours need both `start` and `end`")
        if self.days is not None and self.start is None:
            raise ValueError("business hours `days` need a `start` and an `end`")
        self.build()
        return self

    def all_windows(self) -> tuple[BusinessHoursWindowConfig, ...]:
        windows = self.windows
        if self.start is not None and self.end is not None:
            first = BusinessHoursWindowConfig(
                days=self.days or ("mon", "tue", "wed", "thu", "fri"),
                start=self.start,
                end=self.end,
            )
            windows = (first, *windows)
        return windows

    def build(self) -> BusinessHours:
        return BusinessHours([w.build() for w in self.all_windows()], self.timezone)


class TriggerConfig(_Strict):
    enabled: bool = False
    token_env: str = "BWEXP_TRIGGER_TOKEN"  # noqa: S105 - the variable's name, not a secret
    min_interval: Duration = 900.0


class BudgetConfig(_Strict):
    # Past the limit, runs are skipped until the period resets. Continuous latency is a
    # blackbox_exporter job, so there is no latency-only fallback.
    limit: Size | None = None
    reset_day: int = Field(default=1, ge=1, le=28)


class _YamlPath:
    """Where `Settings` reads its YAML from; set by `load_settings`."""

    path: ClassVar[Path | None] = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BWEXP_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    listen: str = f"0.0.0.0:{DEFAULT_PORT}"
    state_dir: Path = Path("/var/lib/bandwidth-exporter")
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["text", "json"] = "text"
    # A test due at start-up waits this long, plus a random share of `startup_jitter` so the
    # pods of a DaemonSet that restart together do not all test at once.
    startup_delay: Duration = 30.0
    startup_jitter: Duration = 0.0
    # Recorded on bandwidth_test_info so results from pod and host networking stay apart.
    network_mode: str = ""
    # This instance's name among its peers; the host name when unset. In a DaemonSet the chart
    # sets it to the node name.
    peer_id: str = ""
    # With several instances (a DaemonSet), only the one with this peer id runs the
    # north/south tests: one tester per egress.
    north_south_on: str = ""
    # Where each instance runs (a site, region or datacenter), by peer id. Results carry the
    # tester's zone and the peer's, so east/west pairs can be read site to site.
    zones: dict[str, str] = Field(default_factory=dict)
    business_hours: BusinessHoursConfig = Field(default_factory=BusinessHoursConfig)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    defaults: Defaults = Field(default_factory=Defaults)
    north_south: tuple[NorthSouthTest, ...] = ()
    east_west: tuple[EastWestTest, ...] = ()
    responder: ResponderConfig = Field(default_factory=ResponderConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if _YamlPath.path is not None:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=_YamlPath.path))
        return tuple(sources)

    @field_validator("listen")
    @classmethod
    def _listen(cls, value: str) -> str:
        parse_host_port(value, default_port=None)
        return value

    @field_validator("peer_id", "north_south_on", mode="before")
    @classmethod
    def _peer_id(cls, value: Any) -> Any:
        if value:
            _check_name(str(value).lower(), "peer ids")
            return str(value).lower()
        return value

    @field_validator("zones", mode="before")
    @classmethod
    def _zones(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        zones = {}
        for peer, zone in value.items():
            peer = _check_name(str(peer).lower(), "peer ids")
            zones[peer] = _check_name(zone, "zone names")
        return zones

    @model_validator(mode="after")
    def _consistency(self) -> Settings:
        seen: set[str] = set()
        for test in (*self.north_south, *self.east_west):
            if test.name in seen:
                raise ValueError(f"duplicate test name {test.name!r}")
            seen.add(test.name)
        hours = self.business_hours.build()
        # Resolve every test now so that combinations (warm-up against max_duration, a cron
        # schedule that only ever fires in business hours) fail at load time.
        for test in self.north_south:
            _check_against_hours(resolve_test(test, self.defaults), hours)
        for test in self.east_west:
            _check_against_hours(
                resolve_east_west(test, self.defaults, "x").pair("y", "y:1"), hours
            )
        if self.responder.enabled and self.responder.max_duration < AUTO_WARMUP_CAP + 3:
            raise ValueError("responder.max_duration must leave room for a warm-up and 3 s")
        return self

    @property
    def listen_host(self) -> str:
        return parse_host_port(self.listen, default_port=None)[0]

    @property
    def listen_port(self) -> int:
        return parse_host_port(self.listen, default_port=None)[1]

    @property
    def own_peer_id(self) -> str:
        if self.peer_id:
            return self.peer_id
        host = socket.gethostname().split(".")[0].lower()
        cleaned = re.sub(r"[^a-z0-9._-]", "-", host).strip("-._") or "localhost"
        return cleaned[:63]

    @property
    def own_zone(self) -> str:
        return self.zones.get(self.own_peer_id, "")

    @property
    def hours(self) -> BusinessHours:
        return self.business_hours.build()

    @property
    def runs_north_south(self) -> bool:
        return not self.north_south_on or self.north_south_on == self.own_peer_id


def _check_against_hours(spec: TestSpec, hours: BusinessHours) -> None:
    schedule = spec.schedule
    if not hours.enabled or not isinstance(schedule, CronSchedule):
        return
    rng = random.Random(0)  # noqa: S311 - a reproducible check, not cryptography
    if next_run(schedule, 1_800_000_000.0, rng, hours, limit=2000) is None:
        raise ConfigError(f"{spec.name}: every run of its cron schedule falls in business hours")


def parse_host_port(value: str, default_port: int | None) -> tuple[str, int]:
    """`host:port`, `[v6]:port`, or a bare host when a default port exists."""
    text = value.strip()
    host: str
    port_text: str | None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            raise ValueError(f"unbalanced brackets in {value!r}")
        host = text[1:end]
        rest = text[end + 1 :]
        if rest and not rest.startswith(":"):
            raise ValueError(f"expected :port after {text[: end + 1]!r}")
        port_text = rest[1:] if rest else None
        ipaddress.IPv6Address(host)
    elif text.count(":") == 1:
        host, port_text = text.split(":")
    elif text.count(":") > 1:
        raise ValueError(f"write IPv6 addresses as [addr]:port, got {value!r}")
    else:
        host, port_text = text, None
    if port_text is None:
        if default_port is None:
            raise ValueError(f"{value!r} needs a port")
        port = default_port
    else:
        if not port_text.isdigit():
            raise ValueError(f"bad port in {value!r}")
        port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range in {value!r}")
    # Bracketed IPv6 was validated above; everything else must look like a host name or IPv4.
    if not host or (":" not in host and not _HOST.match(host)):
        raise ValueError(f"bad host in {value!r}")
    return host, port


def format_host_port(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True)
class TestSpec:
    """A test with every default applied: what the scheduler and the worker act on. An
    east/west test becomes one TestSpec per peer."""

    name: str
    kind: str
    peer: str
    backend: str
    target: str
    schedule: Schedule
    warmup: float | None  # None means "auto": end the warm-up when the rate is stable
    duration: float
    max_duration: float
    streams: int
    directions: tuple[str, ...]
    early_stop: bool
    plan_download: float | None
    plan_upload: float | None
    ip_family: str
    bind_address: str | None
    congestion_control: str | None
    options: dict[str, Any] = field(default_factory=dict)
    # East/west only: who we are, and the env var with the key we sign requests with.
    self_id: str = ""
    key_env: str = ""
    # From `zones`: where the tester runs, and (east/west) where the peer does.
    zone: str = ""
    peer_zone: str = ""

    # Never collected by pytest even though the name starts with "Test".
    __test__: ClassVar[bool] = False

    @property
    def labels(self) -> tuple[str, str, str, str, str]:
        return (self.name, self.kind, self.peer, self.zone, self.peer_zone)

    @property
    def key(self) -> str:
        """Unique per series: the test, plus the peer for east/west."""
        return f"{self.name}@{self.peer}" if self.peer else self.name

    def hard_timeout(self) -> float:
        """Wall-clock bound for one run, after which the worker process is killed."""
        per_direction = self.max_duration + 30.0
        return 60.0 + per_direction * len(self.directions)

    def worker_spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "peer": self.peer,
            "backend": self.backend,
            "target": self.target,
            "warmup": self.warmup,
            "duration": self.duration,
            "max_duration": self.max_duration,
            "streams": self.streams,
            "directions": list(self.directions),
            "early_stop": self.early_stop,
            "ip_family": self.ip_family,
            "bind_address": self.bind_address,
            "congestion_control": self.congestion_control,
            "options": self.options,
            "self_id": self.self_id,
            "key_env": self.key_env,
        }


def _resolved_common(test: _TestCommon, defaults: Defaults, backend: str) -> dict[str, Any]:
    warmup = test.warmup if test.warmup is not None else defaults.warmup
    duration = test.duration if test.duration is not None else defaults.duration
    max_duration = test.max_duration if test.max_duration is not None else defaults.max_duration
    warmup_seconds = None if warmup == "auto" else float(warmup)
    warmup_cap = AUTO_WARMUP_CAP if warmup_seconds is None else warmup_seconds
    if duration < 1:
        raise ConfigError(f"{test.name}: duration must be at least 1s")
    if max_duration < warmup_cap + min(duration, 3.0):
        raise ConfigError(
            f"{test.name}: max_duration ({max_duration:g}s) leaves too little time to measure "
            f"after a warm-up of up to {warmup_cap:g}s"
        )
    plan = test.plan or PlanConfig()
    return {
        "name": test.name,
        "backend": backend,
        "schedule": (test.schedule or defaults.schedule).build(),
        "warmup": warmup_seconds,
        "duration": duration,
        "max_duration": max_duration,
        "streams": test.streams if test.streams is not None else defaults.streams,
        "directions": test.directions if test.directions is not None else defaults.directions,
        "early_stop": test.early_stop if test.early_stop is not None else defaults.early_stop,
        "plan_download": plan.download,
        "plan_upload": plan.upload,
        "ip_family": test.ip_family,
        "bind_address": test.bind_address,
        "congestion_control": test.congestion_control,
        "options": BACKEND_OPTIONS[backend].model_validate(test.options).model_dump(),
    }


def resolve_test(test: NorthSouthTest, defaults: Defaults, zone: str = "") -> TestSpec:
    common = _resolved_common(test, defaults, test.backend)
    if test.backend == "cloudflare":
        target = common["options"]["base_url"].split("://", 1)[1].split("/", 1)[0]
    else:
        target = format_host_port(*parse_host_port(test.target or "", default_port=5201))
    return TestSpec(kind="north_south", peer="", target=target, zone=zone, **common)


@dataclass(frozen=True)
class EastWestPlan:
    """An east/west test before its peers are known: `pair` makes one TestSpec per peer."""

    template: TestSpec
    peers: tuple[StaticPeer, ...]
    discovery: DnsDiscovery | None
    random_peers: int | None
    max_peers: int
    zones: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.template.name

    def pair(self, peer_id: str, address: str) -> TestSpec:
        return replace(
            self.template, peer=peer_id, target=address, peer_zone=self.zones.get(peer_id, "")
        )


def resolve_east_west(
    test: EastWestTest,
    defaults: Defaults,
    self_id: str,
    zones: Mapping[str, str] | None = None,
) -> EastWestPlan:
    zones = zones or {}
    common = _resolved_common(test, defaults, test.backend)
    template = TestSpec(
        kind="east_west",
        peer="",
        target="",
        self_id=self_id,
        key_env=test.auth.key_env,
        zone=zones.get(self_id, ""),
        **common,
    )
    return EastWestPlan(
        template=template,
        peers=test.peers,
        discovery=test.discovery,
        random_peers=test.topology.random_peers,
        max_peers=test.max_peers,
        zones=zones,
    )


def load_settings(path: Path | None) -> Settings:
    """Read the YAML file (if any) and the environment. Raises ConfigError with a readable
    message on any problem."""
    if path is not None and not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    _YamlPath.path = path
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from None
    finally:
        _YamlPath.path = None


def enabled_tests(settings: Settings) -> list[TestSpec]:
    """North/south tests this instance runs."""
    if not settings.runs_north_south:
        return []
    zone = settings.own_zone
    return [resolve_test(t, settings.defaults, zone) for t in settings.north_south if t.enabled]


def east_west_plans(settings: Settings) -> list[EastWestPlan]:
    me = settings.own_peer_id
    return [
        resolve_east_west(t, settings.defaults, me, settings.zones)
        for t in settings.east_west
        if t.enabled
    ]


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["invalid configuration:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {location}: {message}")
    return "\n".join(lines)
