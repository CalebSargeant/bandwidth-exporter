#!/usr/bin/env python3
"""Snapshot PyPI metadata (latest version, release dates, requires_python, classifiers) for candidate libraries.
Usage: check_pypi.py > results/pypi_snapshot.json
"""
import json
import re
import sys
import urllib.request

PKGS = ["prometheus-client", "APScheduler", "croniter", "cronsim", "aiohttp", "httpx", "uvloop", "speedtest-cli",
        "iperf3", "pydantic-settings", "fastapi", "uvicorn", "starlette", "ndt7", "ndt7-client", "pyndt7",
        "ndt7-python", "python-ndt7", "speedtest", "ookla-speedtest", "cloudflarepycli", "cfspeedtest"]


def get(name):
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=20) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def main():
    out = {}
    for name in PKGS:
        j = get(name)
        if "error" in j:
            out[name] = {"error": j["error"]}
            continue
        info = j["info"]
        rel = []
        for ver, files in j["releases"].items():
            if files:
                t = min(f["upload_time_iso_8601"] for f in files)
                rel.append((t, ver, any(f.get("yanked") for f in files)))
        rel.sort()
        pyver = sorted({c.split("::")[-1].strip() for c in info.get("classifiers", [])
                        if re.match(r"Programming Language :: Python :: 3\.\d+$", c)}, key=lambda v: int(v.split(".")[1]))
        status = [c.split("::")[-1].strip() for c in info.get("classifiers", []) if c.startswith("Development Status")]
        out[name] = {
            "latest": info["version"],
            "latest_uploaded": next((t for t, v, _ in reversed(rel) if v == info["version"]), None),
            "requires_python": info.get("requires_python"),
            "python_classifiers": pyver,
            "dev_status": status,
            "license": info.get("license_expression") or (info.get("license") or "")[:60],
            "summary": info.get("summary"),
            "project_urls": info.get("project_urls"),
            "first_release": rel[0][:2] if rel else None,
            "last_8_releases": [f"{v} ({t[:10]}){' YANKED' if y else ''}" for t, v, y in rel[-8:]],
        }
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
