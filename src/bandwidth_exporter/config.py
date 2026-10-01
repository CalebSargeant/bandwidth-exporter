"""Configuration: a YAML file for the test list, environment variables for scalars and secrets.

Loading validates everything up front (durations, cron expressions, backend options, names
that become label values) and rejects unknown keys, so a typo fails at start-up instead of
silently changing what gets measured. Secrets never live in the file: the file names the
environment variable that holds them.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
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

from .schedules import CronSchedule, RandomSchedule, Schedule
from .units import parse_bytes, parse_duration, parse_rate

DEFAULT_PORT = 10056
DEFAULT_CONFIG_PATH = Path("/etc/bandwidth-exporter/config.yaml")

Duration = Annotated[float, BeforeValidator(parse_duration)]
Size = Annotated[int, BeforeValidator(parse_bytes)]
Rate = Annotated[float, BeforeValidator(parse_rate)]

# Test names become the `test` label on every series, so they are short, stable and boring.
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
# Hostnames that may reach a command line (iperf3 -c). No leading dash, no spaces, no shell.
_HOST = re.compile(r"^(?!-)[A-Za-z0-9.-]{1,253}$")
_CCA = re.compile(r"^[a-z0-9_]{1,32}$")

# Cloudflare answers 403 to `__down?bytes=` of 100 MB and more (observed 2026-10-01).
CLOUDFLARE_MAX_DOWNLOAD_CHUNK = 99_999_999


class ConfigError(ValueError):
    """The configuration cannot be used. The message says what to fix."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


BACKEND_OPTIONS: dict[str, type[_Strict]] = {
    "cloudflare": CloudflareOptions,
    "iperf3": Iperf3Options,
}


class NorthSouthTest(_Strict):
    name: str
    # `engine` is accepted as a synonym: the design document uses both words.
    backend: Literal["cloudflare", "iperf3"]
    enabled: bool = True
    target: str | None = None
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
        if isinstance(data, dict) and "engine" in data:
            if "backend" in data:
                raise ValueError("set `backend` or `engine`, not both")
            data = {**data, "backend": data["engine"]}
            del data["engine"]
        return data

    @field_validator("name", mode="before")
    @classmethod
    def _yaml_boolean_name(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("quote the name: YAML reads on, off, yes and no as true or false")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError(
                "test names are lower-case letters, digits, '.', '_' and '-', "
                "up to 63 characters, starting with a letter or digit"
            )
        return value

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


class TriggerConfig(_Strict):
    enabled: bool = False
    token_env: str = "BWEXP_TRIGGER_TOKEN"  # noqa: S105 - the variable's name, not a secret
    min_interval: Duration = 900.0


class BudgetConfig(_Strict):
    limit: Size | None = None
    reset_day: int = Field(default=1, ge=1, le=28)
    on_exhausted: Literal["latency_only", "skip"] = "latency_only"


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
    startup_delay: Duration = 30.0
    # Recorded on bandwidth_test_info so results from pod and host networking stay apart.
    network_mode: str = ""
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    defaults: Defaults = Field(default_factory=Defaults)
    north_south: tuple[NorthSouthTest, ...] = ()
    east_west: tuple[Any, ...] = ()
    responder: dict[str, Any] | None = None

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

    @field_validator("east_west")
    @classmethod
    def _east_west(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        if value:
            raise ValueError(
                "east/west tests arrive with the responder role (roadmap phase 1); "
                "this version runs north/south tests only"
            )
        return value

    @field_validator("responder")
    @classmethod
    def _responder(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value and value.get("enabled"):
            raise ValueError("the responder role is not in this version (roadmap phase 1)")
        return value

    @model_validator(mode="after")
    def _unique_names(self) -> Settings:
        seen: set[str] = set()
        for test in self.north_south:
            if test.name in seen:
                raise ValueError(f"duplicate test name {test.name!r}")
            seen.add(test.name)
        # Resolve every test now so that combinations (warm-up against max_duration) fail
        # at load time.
        for test in self.north_south:
            resolve_test(test, self.defaults)
        return self

    @property
    def listen_host(self) -> str:
        return parse_host_port(self.listen, default_port=None)[0]

    @property
    def listen_port(self) -> int:
        return parse_host_port(self.listen, default_port=None)[1]


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


@dataclass(frozen=True)
class TestSpec:
    """A test with every default applied: what the scheduler and the worker act on."""

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

    # Never collected by pytest even though the name starts with "Test".
    __test__: ClassVar[bool] = False

    @property
    def labels(self) -> tuple[str, str, str]:
        return (self.name, self.kind, self.peer)

    def hard_timeout(self) -> float:
        """Wall-clock bound for one run, after which the worker process is killed."""
        per_direction = self.max_duration + 30.0
        return 60.0 + per_direction * len(self.directions)

    def worker_spec(self, *, latency_only: bool = False) -> dict[str, Any]:
        return {
            "name": self.name,
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
            "latency_only": latency_only,
            "options": self.options,
        }


def resolve_test(test: NorthSouthTest, defaults: Defaults) -> TestSpec:
    warmup = test.warmup if test.warmup is not None else defaults.warmup
    duration = test.duration if test.duration is not None else defaults.duration
    max_duration = test.max_duration if test.max_duration is not None else defaults.max_duration
    options_model = BACKEND_OPTIONS[test.backend].model_validate(test.options)
    warmup_seconds = None if warmup == "auto" else float(warmup)
    # The automatic warm-up ends at the latest after 5 s; leave at least 3 s to measure.
    warmup_cap = 5.0 if warmup_seconds is None else warmup_seconds
    if duration < 1:
        raise ConfigError(f"{test.name}: duration must be at least 1s")
    if max_duration < warmup_cap + min(duration, 3.0):
        raise ConfigError(
            f"{test.name}: max_duration ({max_duration:g}s) leaves too little time to measure "
            f"after a warm-up of up to {warmup_cap:g}s"
        )
    if isinstance(options_model, CloudflareOptions):
        target = options_model.base_url.split("://", 1)[1].split("/", 1)[0]
    else:
        host, port = parse_host_port(test.target or "", default_port=5201)
        target = f"{host}:{port}" if ":" not in host else f"[{host}]:{port}"
    plan = test.plan or PlanConfig()
    return TestSpec(
        name=test.name,
        kind="north_south",
        peer="",
        backend=test.backend,
        target=target,
        schedule=(test.schedule or defaults.schedule).build(),
        warmup=warmup_seconds,
        duration=duration,
        max_duration=max_duration,
        streams=test.streams if test.streams is not None else defaults.streams,
        directions=test.directions if test.directions is not None else defaults.directions,
        early_stop=test.early_stop if test.early_stop is not None else defaults.early_stop,
        plan_download=plan.download,
        plan_upload=plan.upload,
        ip_family=test.ip_family,
        bind_address=test.bind_address,
        congestion_control=test.congestion_control,
        options=options_model.model_dump(),
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
    return [resolve_test(t, settings.defaults) for t in settings.north_south if t.enabled]


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["invalid configuration:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {location}: {message}")
    return "\n".join(lines)
