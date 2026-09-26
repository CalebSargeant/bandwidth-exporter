#!/usr/bin/env python3
"""Benchmark harness: runs each scenario N times over loopback and records throughput and CPU.

For each run it records
  * throughput (receiver-side byte count / client-measured transfer time),
  * client CPU seconds (self-reported getrusage from just before the transfer; wait4 rusage for iperf3),
  * server CPU seconds (psutil user+system delta of the long-lived server process, all threads),
  * system-wide busy CPU from /proc/stat (catches softirq work not charged to either process).
Derived: cores used per side, and total "CPU cores per Gbit/s" (== CPU-seconds per Gbit moved).

Constraints (--constraint):
  none            : unconstrained (4 vCPUs shared by both ends)
  quota-0.5/1.0   : each end in its own cgroup-v1 cpu cgroup with cfs_quota = 0.5/1.0 x cfs_period (100 ms);
                    this is the same CFS bandwidth mechanism behind `docker run --cpus` and Kubernetes limits.cpu
  pin-shared      : both ends pinned to the same single core with taskset (whole test on 1 core)

Usage: run_bench.py --group tcp|http|tls|constrained [--runs 3] [--seconds S] [--only substr]
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv", "bin", "python")
GOB = os.path.join(HERE, "bin", "gobench")
IPERF_APT = "/usr/bin/iperf3"
IPERF_NEW = os.environ.get("IPERF3_NEW", "/tmp/iperf3-build/install/bin/iperf3")
RESULTS = os.path.join(HERE, "results")
CG_ROOT = "/sys/fs/cgroup/cpu"


# ------------------------------------------------------------------ scenario builders
def iperf(name, binary, parallel, port=5201):
    return dict(name=name, kind="iperf3", direction="UP", port=port,
                server=[binary, "-s", "-p", str(port)],
                client=lambda s: [binary, "-c", "127.0.0.1", "-p", str(port), "-t", str(int(s)), "-P", str(parallel), "-J"])


def pytcp(name, impl, parallel=1, direction="UP", port=5301, chunk=None):
    extra = ["--chunk", str(chunk)] if chunk else []
    return dict(name=name, kind="json", direction=direction, port=port,
                server=[PY, os.path.join(HERE, "py_tcp.py"), "server", "--impl", impl, "--port", str(port)],
                client=lambda s: [PY, os.path.join(HERE, "py_tcp.py"), "client", "--impl", impl, "--port", str(port),
                                  "--dir", direction, "--seconds", str(s), "--parallel", str(parallel)] + extra)


def gotcp(name, parallel=1, direction="UP", port=5302):
    return dict(name=name, kind="json", direction=direction, port=port,
                server=[GOB, "tcp-server", "-port", str(port)],
                client=lambda s: [GOB, "tcp-client", "-port", str(port), "-dir", direction, "-t", str(s), "-P", str(parallel)])


def http(name, srv, cli, direction, tls=False, port=8081, loop=None):
    t = (["--tls"] if tls else []) + (["--loop", loop] if loop and cli != "go" else [])
    if srv == "go":
        server = [GOB, "http-server", "-port", str(port)] + (["-tls"] if tls else [])
    else:
        server = [PY, os.path.join(HERE, "py_http_server.py"), "--impl", srv, "--port", str(port)] + t
    if cli == "go":
        client = lambda s: [GOB, "http-client", "-port", str(port), "-dir", direction, "-t", str(s)] + (["-tls"] if tls else [])
    else:
        client = lambda s: [PY, os.path.join(HERE, "py_http_client.py"), "--impl", cli, "--port", str(port),
                            "--dir", direction, "--seconds", str(s)] + t
    return dict(name=name, kind="json", direction="UP" if direction == "up" else "DOWN", port=port,
                server=server, client=client)


def scenarios(group):
    if group == "tcp":
        return [
            iperf("iperf3-3.21 TCP P1", IPERF_NEW, 1),
            iperf("iperf3-3.21 TCP P4", IPERF_NEW, 4),
            iperf("iperf3-3.16(apt) TCP P1", IPERF_APT, 1, port=5202),
            iperf("iperf3-3.16(apt) TCP P4", IPERF_APT, 4, port=5202),
            pytcp("py blocking sendall/recv_into P1", "blocking"),
            pytcp("py blocking sendall/recv_into P4 (threads)", "blocking", parallel=4),
            pytcp("py os.sendfile/recv_into P1", "sendfile"),
            pytcp("py asyncio sock_sendall/sock_recv_into P1", "asyncio"),
            pytcp("py uvloop sock_sendall/sock_recv_into P1", "uvloop"),
            pytcp("py uvloop Transport/BufferedProtocol P1", "proto"),
            gotcp("go net.Conn TCP P1"),
            gotcp("go net.Conn TCP P4", parallel=4),
        ]
    if group == "http":
        out = []
        for d in ("down", "up"):
            for srv, cli in [("aiohttp", "aiohttp"), ("uvicorn", "httpx"), ("uvicorn", "aiohttp"), ("go", "go"),
                             ("go", "aiohttp"), ("go", "httpx"), ("aiohttp", "go"), ("uvicorn", "go")]:
                out.append(http(f"HTTP {d} srv={srv} cli={cli}", srv, cli, d))
        return out
    if group == "tls":
        return [http(f"HTTPS down srv={s} cli={c}", s, c, "down", tls=True, port=8443)
                for s, c in [("go", "go"), ("go", "aiohttp"), ("go", "httpx"), ("aiohttp", "aiohttp"),
                             ("aiohttp", "go"), ("uvicorn", "go")]]
    if group == "constrained":
        return [
            iperf("iperf3-3.21 TCP P1", IPERF_NEW, 1),
            iperf("iperf3-3.21 TCP P4", IPERF_NEW, 4),
            pytcp("py blocking sendall/recv_into P1", "blocking"),
            pytcp("py uvloop sock_sendall/sock_recv_into P1", "uvloop"),
            http("HTTP down srv=aiohttp cli=aiohttp", "aiohttp", "aiohttp", "down"),
            http("HTTP down srv=uvicorn cli=httpx", "uvicorn", "httpx", "down"),
            http("HTTP down srv=go cli=go", "go", "go", "down"),
        ]
    if group == "extras":
        return [
            pytcp("py blocking P1 chunk=64KiB", "blocking", chunk=65536),
            pytcp("py blocking P1 chunk=16KiB", "blocking", chunk=16384),
            pytcp("py uvloop sock_* P1 chunk=64KiB", "uvloop", chunk=65536),
            http("HTTP down srv=go cli=aiohttp loop=asyncio(stock)", "go", "aiohttp", "down", loop="asyncio"),
            http("HTTP down srv=go cli=httpx loop=asyncio(stock)", "go", "httpx", "down", loop="asyncio"),
        ]
    raise SystemExit(f"unknown group {group}")


# ------------------------------------------------------------------ constraint wrappers
def make_cgroup(name, cpus):
    path = os.path.join(CG_ROOT, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "cpu.cfs_period_us"), "w") as f:
        f.write("100000")
    with open(os.path.join(path, "cpu.cfs_quota_us"), "w") as f:
        f.write(str(int(cpus * 100000)))
    return path


def drop_cgroup(path):
    try:
        os.rmdir(path)
    except OSError:
        pass


def wrap(cmd, constraint, side):
    if constraint == "none":
        return cmd
    if constraint == "pin-shared":
        return ["taskset", "-c", "1"] + cmd
    if constraint.startswith("quota-"):
        cg = os.path.join(CG_ROOT, f"bwbench_{side}")
        return ["sh", "-c", f'echo $$ > {cg}/cgroup.procs && exec "$@"', "sh"] + cmd
    raise SystemExit(constraint)


# ------------------------------------------------------------------ measurement helpers
def proc_stat_busy():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v) - idle


def wait_listen(port, timeout=15):
    t = time.time() + timeout
    while time.time() < t:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                return True
        time.sleep(0.1)
    return False


def cpu_of(p):
    ct = p.cpu_times()
    return ct.user + ct.system


def parse(kind, text):
    if kind == "iperf3":
        j = json.loads(text)
        e = j["end"]
        rcv = e["sum_received"]
        return dict(bytes=rcv["bytes"], seconds=rcv["seconds"], gbps=rcv["bits_per_second"] / 1e9,
                    sender_gbps=e["sum_sent"]["bits_per_second"] / 1e9,
                    retransmits=e["sum_sent"].get("retransmits"),
                    iperf_cpu=e.get("cpu_utilization_percent"),
                    version=j["start"].get("version"), client_cpu_self_s=None)
    line = [l for l in text.strip().splitlines() if l.startswith("{")][-1]
    return json.loads(line)


def run_one(sc, seconds, constraint, server_proc):
    busy0 = proc_stat_busy()
    s0 = cpu_of(server_proc)
    with tempfile.TemporaryFile() as out:
        t0 = time.monotonic()
        p = subprocess.Popen(wrap(sc["client"](seconds), constraint, "cli"), stdout=out, stderr=subprocess.PIPE)
        _, status, ru = os.wait4(p.pid, 0)
        wall = time.monotonic() - t0
        out.seek(0)
        text = out.read().decode()
    s1 = cpu_of(server_proc)
    busy1 = proc_stat_busy()
    if status != 0 or not text.strip():
        return dict(error=f"client exit {status}", stderr=p.stderr.read().decode()[-2000:])
    r = parse(sc["kind"], text)
    cli_rusage = ru.ru_utime + ru.ru_stime
    cli_cpu = r.get("client_cpu_self_s") if r.get("client_cpu_self_s") is not None else cli_rusage
    srv_cpu = s1 - s0
    secs = r["seconds"]
    gbit = r["bytes"] * 8 / 1e9
    hz = os.sysconf("SC_CLK_TCK")
    rec = dict(gbps=r["gbps"], bytes=r["bytes"], seconds=secs, wall=wall,
               client_cpu_s=cli_cpu, client_rusage_s=cli_rusage, server_cpu_s=srv_cpu,
               client_cores=cli_cpu / secs, server_cores=srv_cpu / secs,
               system_busy_cores=(busy1 - busy0) / hz / wall,
               cores_per_gbps=(cli_cpu + srv_cpu) / gbit if gbit else None)
    up = sc["direction"] == "UP"
    rec["sender_cores"] = rec["client_cores"] if up else rec["server_cores"]
    rec["receiver_cores"] = rec["server_cores"] if up else rec["client_cores"]
    for k in ("sender_gbps", "retransmits", "iperf_cpu", "version"):
        if k in r:
            rec[k] = r[k]
    return rec


def summarize(name, constraint, runs):
    ok = [r for r in runs if "error" not in r]
    if not ok:
        return dict(name=name, constraint=constraint, n=0)

    def m(k):
        vals = [r[k] for r in ok if r.get(k) is not None]
        return (statistics.mean(vals), statistics.pstdev(vals) if len(vals) > 1 else 0.0) if vals else (None, None)

    s = dict(name=name, constraint=constraint, n=len(ok))
    for k in ("gbps", "sender_cores", "receiver_cores", "system_busy_cores", "cores_per_gbps"):
        s[k], s[k + "_sd"] = m(k)
    s["gbps_min"] = min(r["gbps"] for r in ok)
    s["gbps_max"] = max(r["gbps"] for r in ok)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--constraints", default="none")
    ap.add_argument("--only", default=None)
    ap.add_argument("--tag", default=None)
    a = ap.parse_args()
    os.makedirs(RESULTS, exist_ok=True)
    tag = a.tag or a.group
    raw_path = os.path.join(RESULTS, f"raw_{tag}.jsonl")
    sum_path = os.path.join(RESULTS, f"summary_{tag}.jsonl")
    host = dict(cpus=os.cpu_count(), kernel=os.uname().release, python=sys.version.split()[0],
                started=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    for constraint in a.constraints.split(","):
        cgs = []
        if constraint.startswith("quota-"):
            cpus = float(constraint.split("-")[1])
            cgs = [make_cgroup("bwbench_srv", cpus), make_cgroup("bwbench_cli", cpus)]
        try:
            for sc in scenarios(a.group):
                if a.only and a.only not in sc["name"]:
                    continue
                srv_log = open(os.path.join(RESULTS, "server_stderr.log"), "a")
                srv = subprocess.Popen(wrap(sc["server"], constraint, "srv"), stdout=subprocess.DEVNULL, stderr=srv_log)
                try:
                    if not wait_listen(sc["port"]):
                        raise RuntimeError(f"server for {sc['name']} did not listen")
                    time.sleep(0.3)
                    sp = psutil.Process(srv.pid)
                    runs = []
                    for i in range(a.runs):
                        rec = run_one(sc, a.seconds, constraint, sp)
                        rec.update(scenario=sc["name"], constraint=constraint, run=i + 1, host=host,
                                   client_cmd=" ".join(sc["client"](a.seconds)), server_cmd=" ".join(sc["server"]))
                        runs.append(rec)
                        with open(raw_path, "a") as f:
                            f.write(json.dumps(rec) + "\n")
                        print(f"[{constraint}] {sc['name']} run {i + 1}: "
                              + (f"{rec['gbps']:.2f} Gbit/s  snd {rec['sender_cores']:.2f} / rcv {rec['receiver_cores']:.2f} cores"
                                 f"  sys {rec['system_busy_cores']:.2f}  {rec['cores_per_gbps']:.3f} cores/Gbps"
                                 if "error" not in rec else rec["error"] + " " + rec.get("stderr", "")), flush=True)
                        time.sleep(1)
                    with open(sum_path, "a") as f:
                        f.write(json.dumps(summarize(sc["name"], constraint, runs)) + "\n")
                finally:
                    srv.terminate()
                    try:
                        srv.wait(5)
                    except subprocess.TimeoutExpired:
                        srv.kill()
                        srv.wait()
                    srv_log.close()
                    time.sleep(0.5)
        finally:
            for cg in cgs:
                drop_cgroup(cg)


if __name__ == "__main__":
    main()
