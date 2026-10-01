"""The worker process boundary and the command line, end to end against the fake edge."""

import sys
import textwrap

from prometheus_client.parser import text_string_to_metric_families

from bandwidth_exporter import cli
from bandwidth_exporter.config import Defaults, NorthSouthTest, resolve_test
from bandwidth_exporter.runner import SubprocessRunner


def fast_test(url, **extra):
    return NorthSouthTest(
        name="cf",
        backend="cloudflare",
        warmup="0.3s",
        duration="1s",
        max_duration="3s",
        streams=1,
        early_stop=False,
        options={
            "base_url": url,
            "allow_insecure_http": True,
            "download_chunk": "2MB",
            "upload_chunk": "2MB",
            "latency_samples": 3,
        },
        **extra,
    )


async def test_subprocess_runner_runs_the_worker(edge):
    _, url = edge
    spec = resolve_test(fast_test(url), Defaults())
    result = await SubprocessRunner().run(spec)
    assert result.status == "success", result.message
    assert result.download.bytes_per_second > 0
    assert result.info["server"] == "AMS"


async def test_subprocess_runner_kills_a_hung_worker(tmp_path):
    spec = resolve_test(fast_test("http://127.0.0.1:9"), Defaults())
    hung = tmp_path / "hung.py"
    hung.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    result = await SubprocessRunner([sys.executable, str(hung)], timeout=0.5).run(spec)
    assert result.status == "failure"
    assert result.reason == "timeout"


async def test_worker_without_output_is_a_tool_error(tmp_path):
    spec = resolve_test(fast_test("http://127.0.0.1:9"), Defaults())
    silent = tmp_path / "silent.py"
    silent.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    result = await SubprocessRunner([sys.executable, str(silent)]).run(spec)
    assert (result.status, result.reason) == ("failure", "tool_error")
    assert "code 3" in result.message
    missing = await SubprocessRunner([str(tmp_path / "missing-python")]).run(spec)
    assert (missing.status, missing.reason) == ("failure", "tool_error")


async def test_garbage_from_the_worker_is_a_tool_error(tmp_path):
    spec = resolve_test(fast_test("http://127.0.0.1:9"), Defaults())
    noisy = tmp_path / "noisy.py"
    noisy.write_text("print('not json')\n", encoding="utf-8")
    result = await SubprocessRunner([sys.executable, str(noisy)]).run(spec)
    assert (result.status, result.reason) == ("failure", "tool_error")


def test_check_config(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(
        textwrap.dedent(
            """
            budget: {limit: 100GB}
            north_south:
              - {name: cloudflare, backend: cloudflare}
            """
        ),
        encoding="utf-8",
    )
    assert cli.main(["--config", str(config), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "cloudflare: cloudflare -> speed.cloudflare.com" in out
    assert "100 GB" in out


def test_bad_config_exits_2(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("north_south: [{name: X}]\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "check-config"]) == 2
    assert "invalid configuration" in capsys.readouterr().err


def test_version(capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip()


def test_run_once_writes_a_textfile(edge, tmp_path):
    _, url = edge
    config = tmp_path / "config.yaml"
    config.write_text(
        textwrap.dedent(
            f"""
            state_dir: {tmp_path / "state"}
            north_south:
              - name: cf
                backend: cloudflare
                warmup: 0.3s
                duration: 1s
                max_duration: 3s
                streams: 1
                early_stop: false
                options:
                  base_url: "{url}"
                  allow_insecure_http: true
                  download_chunk: 2MB
                  upload_chunk: 2MB
                  latency_samples: 3
            """
        ),
        encoding="utf-8",
    )
    textfile = tmp_path / "bandwidth.prom"
    assert cli.main(["--config", str(config), "run", "--once", "--textfile", str(textfile)]) == 0
    families = {f.name: f for f in text_string_to_metric_families(textfile.read_text())}
    assert families["bandwidth_download_bytes_per_second"].samples[0].value > 0
    assert families["bandwidth_last_test_success"].samples[0].value == 1
    assert "process_cpu_seconds" not in families
    assert (tmp_path / "state" / "state.json").is_file()


def test_run_once_unknown_test(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"state_dir: {tmp_path}\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "run", "--once", "--test", "nope"]) == 2
