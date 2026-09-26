#!/usr/bin/env python3
"""Render results/raw_*.jsonl and results/docker_check.jsonl as Markdown tables (mean, sd, min-max over runs)."""
import collections
import glob
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")


def ms(vals, fmt="{:.2f}"):
    vals = [v for v in vals if v is not None]
    if not vals:
        return "n/a"
    m = statistics.mean(vals)
    sd = statistics.pstdev(vals) if len(vals) > 1 else 0
    return (fmt + " ± " + fmt).format(m, sd)


def table(rows):
    groups = collections.OrderedDict()
    for r in rows:
        if "error" in r:
            continue
        groups.setdefault((r["constraint"], r["scenario"]), []).append(r)
    out = ["| constraint | scenario | n | Gbit/s mean ± sd | min to max | sender cores | receiver cores | system busy cores | cores per Gbit/s |",
           "|---|---|---|---|---|---|---|---|---|"]
    for (c, s), rs in groups.items():
        g = [r["gbps"] for r in rs]
        out.append(f"| {c} | {s} | {len(rs)} | {ms(g)} | {min(g):.2f} to {max(g):.2f} | {ms([r['sender_cores'] for r in rs])} | "
                   f"{ms([r['receiver_cores'] for r in rs])} | {ms([r['system_busy_cores'] for r in rs])} | "
                   f"{ms([r['cores_per_gbps'] for r in rs], '{:.3f}')} |")
    return "\n".join(out)


def main():
    for path in sorted(glob.glob(os.path.join(RES, "raw_*.jsonl"))):
        rows = [json.loads(l) for l in open(path) if l.strip()]
        errs = [r for r in rows if "error" in r]
        print(f"\n### {os.path.basename(path)}\n")
        print(table(rows))
        if errs:
            print(f"\n{len(errs)} failed run(s): " + "; ".join(e.get("scenario", "?") + ": " + e["error"] for e in errs))
    dpath = os.path.join(RES, "docker_check.jsonl")
    if os.path.exists(dpath):
        rows = [json.loads(l) for l in open(dpath) if l.strip()]
        groups = collections.OrderedDict()
        for r in rows:
            groups.setdefault((r["docker_cpus"], r["scenario"]), []).append(r["gbps"])
        print("\n### docker_check.jsonl (docker run --cpus=X for BOTH server and client containers)\n")
        print("| docker --cpus | scenario | n | Gbit/s mean ± sd | min to max |\n|---|---|---|---|---|")
        for (c, s), g in groups.items():
            print(f"| {c} | {s} | {len(g)} | {ms(g)} | {min(g):.2f} to {max(g):.2f} |")


if __name__ == "__main__":
    main()
