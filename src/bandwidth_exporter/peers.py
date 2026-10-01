"""Which peers each east/west test measures.

Static peers come from configuration. DNS discovery resolves a name (a headless Service returns
one address per ready pod) and asks each address's responder who it is, because the `peer`
label must be a stable identity such as a node name, never an address that changes with every
rollout. A full mesh grows as N x (N-1) pairs, so by default each agent tests only k peers,
chosen by rendezvous hashing: stable across restarts, and only the pairs that involve a peer
that came or went change.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import logging
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .config import EastWestPlan, format_host_port
from .control import ControlClient, ControlError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Peer:
    id: str
    address: str  # host:port of its responder's control API


def rendezvous(self_id: str, peers: Sequence[Peer], k: int | None) -> list[Peer]:
    """The k peers with the highest score for this agent. Every agent ranks differently, so
    the load spreads; a peer joining or leaving moves only the pairs that involve it."""

    def score(peer: Peer) -> int:
        digest = hashlib.sha256(f"{self_id}\0{peer.id}".encode()).digest()
        return int.from_bytes(digest[:8], "big")

    ranked = sorted(peers, key=score, reverse=True)
    return ranked if k is None else ranked[:k]


def resolve_addresses(name: str) -> list[str]:
    infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


InfoFetcher = Callable[[str], str | None]


class Directory:
    def __init__(
        self,
        plans: Sequence[EastWestPlan],
        self_id: str,
        key: Callable[[str], bytes | None],
        *,
        resolver: Callable[[str], list[str]] = resolve_addresses,
        fetch_id: InfoFetcher | None = None,
    ) -> None:
        self.plans = list(plans)
        self.self_id = self_id
        self.key = key
        self.resolver = resolver
        self.fetch_id = fetch_id

    def _identify(self, plan: EastWestPlan, address: str) -> str | None:
        if self.fetch_id is not None:
            return self.fetch_id(address)
        key = self.key(plan.template.key_env)
        if key is None:
            return None
        client = ControlClient(address, self.self_id, key, timeout=3.0)
        try:
            return str(client.info()["peer_id"])
        except (ControlError, OSError, http.client.HTTPException, KeyError, ValueError) as exc:
            log.info("%s: no usable answer from %s (%s)", plan.name, address, exc)
            return None

    def _candidates(self, plan: EastWestPlan) -> list[Peer]:
        if plan.discovery is None:
            return [Peer(p.id, p.address) for p in plan.peers if p.id != self.self_id]
        try:
            addresses = self.resolver(plan.discovery.dns)
        except OSError as exc:
            log.warning("%s: cannot resolve %s: %s", plan.name, plan.discovery.dns, exc)
            return []
        # Asked on every refresh, not cached: a pod IP can come back on another node.
        peers: dict[str, Peer] = {}
        for ip in addresses:
            address = format_host_port(ip, plan.discovery.port)
            peer_id = self._identify(plan, address)
            if peer_id is not None and peer_id != self.self_id:
                peers.setdefault(peer_id, Peer(peer_id, address))
        return list(peers.values())

    def select(self, plan: EastWestPlan) -> list[Peer]:
        chosen = rendezvous(self.self_id, self._candidates(plan), plan.random_peers)
        return chosen[: plan.max_peers]

    async def refresh(self) -> dict[str, list[Peer]]:
        """Each plan's peers for this agent. Blocking lookups run in a thread."""
        result: dict[str, list[Peer]] = {}
        for plan in self.plans:
            result[plan.name] = await asyncio.to_thread(self.select, plan)
        return result

    def interval(self) -> float:
        intervals = [p.discovery.refresh for p in self.plans if p.discovery is not None]
        return min(intervals) if intervals else 0.0
