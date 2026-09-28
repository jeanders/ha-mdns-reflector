#!/usr/bin/env python3
"""mDNS proxy: learn services on source VLANs, re-publish them on target VLANs.

Reflector mode relays multicast packets between VLANs. That fails on networks
whose Wi-Fi never delivers downstream multicast to clients (controller-based
Wi-Fi such as Cisco FlexConnect local switching does this): clients only ever
receive *unicast* mDNS replies to their own queries.

Proxy mode answers like a switch's service-discovery gateway instead. It
learns services on the source interfaces and registers copies of them on the
target interfaces, pointing at the real device's address. Apple devices start
every lookup with a query that requests a unicast reply (the QU bit), and
python-zeroconf honours it, so the answer reaches clients that multicast
cannot.

Decision logic lives in plain functions at the top of this file so it can be
tested without a network; see tests/test_proxy_logic.py.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass, field

log = logging.getLogger("mdns-proxy")

# Linux socket option: when 1 (the default) a socket bound to the mDNS port
# receives multicast for every group joined anywhere on the host, whatever
# interface it arrived on. Not exposed by Python's socket module.
IP_MULTICAST_ALL = 49

BASE = "base"


# --------------------------------------------------------------------------- #
# Pure logic                                                                  #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Iface:
    name: str
    ip: str
    network: ipaddress.IPv4Network

    def contains(self, addr: str) -> bool:
        try:
            return ipaddress.IPv4Address(addr) in self.network
        except ipaddress.AddressValueError:
            return False


def normalize_type(service_type: str) -> str:
    """'_ipp._tcp', '_ipp._tcp.local' and '_ipp._tcp.local.' all become '_ipp._tcp.local.'."""
    t = service_type.strip().rstrip(".")
    if not t.endswith(".local"):
        t += ".local"
    return t + "."


def split_type(browsed_type: str) -> tuple[str, str]:
    """Split a browsed type into (label, base type).

    '_universal._sub._ipp._tcp.local.' -> ('_universal', '_ipp._tcp.local.')
    '_ipp._tcp.local.'                  -> ('base', '_ipp._tcp.local.')
    """
    if "._sub." in browsed_type:
        label, base = browsed_type.split("._sub.", 1)
        return label, base
    return BASE, browsed_type


def variant_type(base_type: str, label: str) -> str:
    """The inverse of split_type."""
    return base_type if label == BASE else f"{label}._sub.{base_type}"


def browse_types(services: list[str], subtypes: list[str]) -> list[str]:
    """Every type the learner must browse: each service plus each of its subtypes."""
    out: list[str] = []
    for svc in services:
        base = normalize_type(svc)
        out.append(base)
        out.extend(variant_type(base, st) for st in subtypes)
    return out


def name_excluded(name: str, server: str | None, patterns: list[str]) -> bool:
    """Case-insensitive substring match against the instance name or host name."""
    haystacks = [name.lower(), (server or "").lower()]
    return any(p.lower() in h for p in patterns if p for h in haystacks)


def plan_targets(
    addresses: list[str],
    sources: list[Iface],
    targets: list[Iface],
    own_ips: set[str],
    exclude_sources: set[str],
) -> tuple[list[Iface], str]:
    """Decide which target interfaces a learned service should be published on.

    Returns (targets, reason). An empty target list means "do not publish",
    and the reason says why.

    A service is only published onto interfaces where it is *not* already
    native. A host with an address on a target's subnet is present there
    itself; publishing a copy would make it see its own name claimed by
    another responder and rename itself (the dual-homed laptop problem).
    """
    if not addresses:
        return [], "no IPv4 address"
    if own_ips.intersection(addresses):
        return [], "advertised by this host itself"
    if exclude_sources.intersection(addresses):
        return [], "source address is excluded"
    if not any(s.contains(a) for s in sources for a in addresses):
        return [], "not on any source interface's subnet"
    allowed = [t for t in targets if not any(t.contains(a) for a in addresses)]
    if not allowed:
        return [], "already native on every target"
    return allowed, "ok"


# --------------------------------------------------------------------------- #
# Host introspection                                                          #
# --------------------------------------------------------------------------- #

def iface_from_host(name: str) -> Iface:
    out = subprocess.run(
        ["ip", "-j", "-4", "addr", "show", "dev", name],
        check=True, capture_output=True, text=True,
    ).stdout
    entries = json.loads(out or "[]")
    for entry in entries:
        for a in entry.get("addr_info", []):
            if a.get("family") == "inet":
                net = ipaddress.IPv4Network(f"{a['local']}/{a['prefixlen']}", strict=False)
                return Iface(name, a["local"], net)
    raise RuntimeError(f"interface {name} has no IPv4 address")


def host_ipv4_addresses() -> set[str]:
    out = subprocess.run(
        ["ip", "-j", "-4", "addr", "show"], check=True, capture_output=True, text=True,
    ).stdout
    return {
        a["local"]
        for entry in json.loads(out or "[]")
        for a in entry.get("addr_info", [])
        if a.get("family") == "inet"
    }


def restrict_multicast_delivery() -> None:
    """Make zeroconf listen sockets receive multicast only on their own interfaces.

    Without this the learner (bound to the source interfaces) also hears the
    copies the publishers announce on the target interfaces. A proxied service
    would then keep itself alive after the real device went away.
    """
    if not sys.platform.startswith("linux"):
        return
    from zeroconf._utils import net as zc_net  # pure-Python module, patchable

    original = zc_net.new_listen_socket

    def listen_socket_joined_groups_only(*args, **kwargs):
        sock = original(*args, **kwargs)
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_IP, IP_MULTICAST_ALL, 0)
            value = sock.getsockopt(socket.IPPROTO_IP, IP_MULTICAST_ALL)
            log.debug("listen socket IP_MULTICAST_ALL=%s", value)
        return sock

    zc_net.new_listen_socket = listen_socket_joined_groups_only


# --------------------------------------------------------------------------- #
# Proxy                                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class Entry:
    base_type: str
    name: str
    addresses: list[str] = field(default_factory=list)
    port: int = 0
    weight: int = 0
    priority: int = 0
    text: bytes = b""
    server: str = ""
    subtypes: set[str] = field(default_factory=set)
    targets: list[Iface] = field(default_factory=list)

    def fingerprint(self) -> tuple:
        return (tuple(self.addresses), self.port, self.text, self.server)


class Proxy:
    def __init__(self, sources, targets, services, subtypes, own_ips, exclude_sources, exclude_names):
        self.sources: list[Iface] = sources
        self.targets: list[Iface] = targets
        self.services = [normalize_type(s) for s in services]
        self.subtypes = list(subtypes)
        self.own_ips: set[str] = own_ips
        self.exclude_sources: set[str] = set(exclude_sources)
        self.exclude_names: list[str] = list(exclude_names)

        self.learner = None
        self.browser = None
        self.publishers: dict[tuple[str, str], object] = {}
        self.learned: dict[str, Entry] = {}
        # (target name, label, instance key) -> (ServiceInfo, fingerprint)
        self.published: dict[tuple[str, str, str], tuple[object, tuple]] = {}
        self.skipped: dict[str, str] = {}
        self.queue: asyncio.Queue = asyncio.Queue()

    # -- lifecycle ----------------------------------------------------------- #

    async def start(self) -> None:
        from zeroconf import DNSQuestionType, IPVersion
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

        self.learner = AsyncZeroconf(
            interfaces=[s.ip for s in self.sources], ip_version=IPVersion.V4Only,
        )
        for target in self.targets:
            for label in [BASE, *self.subtypes]:
                self.publishers[(target.name, label)] = AsyncZeroconf(
                    interfaces=[target.ip], ip_version=IPVersion.V4Only,
                )

        types = browse_types(self.services, self.subtypes)
        log.info("Learning on %s: %s", ", ".join(s.name for s in self.sources), " ".join(types))
        log.info("Publishing on %s", ", ".join(t.name for t in self.targets))
        # QM, not QU: a unicast reply to the learner would be load-balanced
        # across every socket bound to UDP 5353 on this host (Home Assistant's
        # zeroconf included) and could land in the wrong one.
        self.browser = AsyncServiceBrowser(
            self.learner.zeroconf, types, handlers=[self._on_change],
            question_type=DNSQuestionType.QM,
        )

    async def stop(self) -> None:
        log.info("Stopping; withdrawing %d published records", len(self.published))
        if self.browser is not None:
            await self.browser.async_cancel()
        for key in list(self.published):
            await self._unpublish(*key)
        for pub in self.publishers.values():
            await pub.async_close()
        if self.learner is not None:
            await self.learner.async_close()

    def _on_change(self, zeroconf, service_type, name, state_change) -> None:
        # Called on zeroconf's loop; serialise all work through one queue.
        self.queue.put_nowait((service_type, name, state_change))

    async def run(self) -> None:
        while True:
            service_type, name, state_change = await self.queue.get()
            try:
                await self._handle(service_type, name, state_change)
            except Exception:  # never let one bad record stop the proxy
                log.exception("Failed handling %s %s", state_change, name)

    async def report(self, every: float = 300) -> None:
        while True:
            await asyncio.sleep(every)
            names = sorted({e.name for e in self.learned.values() if e.targets})
            log.info("Proxying %d service(s): %s", len(names), "; ".join(names) or "none")

    # -- event handling ------------------------------------------------------ #

    async def _handle(self, browsed_type: str, name: str, state_change) -> None:
        from zeroconf import DNSQuestionType, IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceInfo

        label, base = split_type(browsed_type)
        key = name.lower()

        if state_change is ServiceStateChange.Removed:
            entry = self.learned.get(key)
            if entry is None:
                return
            if label == BASE:
                log.info("Gone from source: %s", name)
                await self._sync(entry, withdraw_all=True)
                del self.learned[key]
            else:
                entry.subtypes.discard(label)
                await self._sync(entry)
            return

        info = AsyncServiceInfo(base, name)
        if not await info.async_request(self.learner.zeroconf, 3000, question_type=DNSQuestionType.QM):
            log.debug("Could not resolve %s", name)
            return

        addresses = info.parsed_addresses(IPVersion.V4Only)
        if name_excluded(name, info.server, self.exclude_names):
            reason = "name is excluded"
            targets = []
        else:
            targets, reason = plan_targets(
                addresses, self.sources, self.targets, self.own_ips, self.exclude_sources,
            )
        if not targets:
            if self.skipped.get(key) != reason:
                log.info("Not proxying %s: %s", name, reason)
                self.skipped[key] = reason
            if key in self.learned:
                await self._sync(self.learned.pop(key), withdraw_all=True)
            return
        self.skipped.pop(key, None)

        entry = self.learned.get(key) or Entry(base_type=base, name=name)
        entry.addresses = sorted(addresses)
        entry.port = info.port or 0
        entry.weight = info.weight or 0
        entry.priority = info.priority or 0
        entry.text = info.text or b""
        entry.server = info.server or ""
        entry.targets = targets
        if label != BASE:
            entry.subtypes.add(label)
        is_new = key not in self.learned
        self.learned[key] = entry
        if is_new:
            log.info("Learned %s -> %s:%s", name, ",".join(entry.addresses), entry.port)
        await self._sync(entry)

    # -- publishing ---------------------------------------------------------- #

    async def _sync(self, entry: Entry, withdraw_all: bool = False) -> None:
        key = entry.name.lower()
        desired = set() if withdraw_all else {
            (t.name, label) for t in entry.targets for label in [BASE, *entry.subtypes]
        }
        current = {(t, l) for (t, l, k) in self.published if k == key}
        for target, label in current - desired:
            await self._unpublish(target, label, key)
        for target, label in desired:
            await self._publish(target, label, entry)

    async def _publish(self, target: str, label: str, entry: Entry) -> None:
        from zeroconf import ServiceInfo

        key = (target, label, entry.name.lower())
        fingerprint = entry.fingerprint()
        existing = self.published.get(key)
        if existing is not None and existing[1] == fingerprint:
            return

        info = ServiceInfo(
            variant_type(entry.base_type, label),
            entry.name,
            addresses=[socket.inet_aton(a) for a in entry.addresses],
            port=entry.port,
            weight=entry.weight,
            priority=entry.priority,
            properties=entry.text,
            server=entry.server,
        )
        publisher = self.publishers[(target, label)]
        if existing is None:
            # cooperating_responders: the real device legitimately owns this
            # name on its own VLAN, so do not probe for or fight conflicts.
            await (await publisher.async_register_service(info, cooperating_responders=True))
            log.info("Published %s on %s%s", entry.name, target,
                     "" if label == BASE else f" (subtype {label})")
        else:
            await (await publisher.async_update_service(info))
            log.info("Updated %s on %s%s", entry.name, target,
                     "" if label == BASE else f" (subtype {label})")
        self.published[key] = (info, fingerprint)

    async def _unpublish(self, target: str, label: str, key: str) -> None:
        existing = self.published.pop((target, label, key), None)
        if existing is None:
            return
        info, _ = existing
        await (await self.publishers[(target, label)].async_unregister_service(info))
        log.info("Withdrew %s from %s%s", info.name, target,
                 "" if label == BASE else f" (subtype {label})")


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

LOG_LEVELS = {
    "debug": logging.DEBUG, "info": logging.INFO, "notice": logging.INFO,
    "warn": logging.WARNING, "error": logging.ERROR, "fatal": logging.CRITICAL,
}


async def amain(options: dict) -> None:
    sources = [iface_from_host(n) for n in options["proxy_sources"]]
    targets = [iface_from_host(n) for n in options["proxy_targets"]]
    for s in sources:
        log.info("Source %s: %s (%s)", s.name, s.ip, s.network)
    for t in targets:
        log.info("Target %s: %s (%s)", t.name, t.ip, t.network)

    restrict_multicast_delivery()
    proxy = Proxy(
        sources=sources,
        targets=targets,
        services=options.get("proxy_services") or [],
        subtypes=options.get("proxy_subtypes") or [],
        own_ips=host_ipv4_addresses(),
        exclude_sources=options.get("exclude_sources") or [],
        exclude_names=options.get("exclude_names") or [],
    )
    await proxy.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    workers = [asyncio.create_task(proxy.run()), asyncio.create_task(proxy.report())]
    await stop.wait()
    for w in workers:
        w.cancel()
    await proxy.stop()


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/data/options.json"
    with open(path) as fh:
        options = json.load(fh)
    level = LOG_LEVELS.get(options.get("log_level", "info"), logging.INFO)
    logging.basicConfig(level=level, format="[%(asctime)s] %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    if level > logging.DEBUG:
        logging.getLogger("zeroconf").setLevel(logging.WARNING)
    asyncio.run(amain(options))


if __name__ == "__main__":
    main()
