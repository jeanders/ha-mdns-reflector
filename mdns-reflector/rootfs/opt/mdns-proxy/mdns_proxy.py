#!/usr/bin/env python3
"""mDNS proxy: learn services on source VLANs, re-publish them on target VLANs.

Reflector mode relays multicast packets between VLANs. That fails on networks
whose Wi-Fi never delivers downstream multicast to clients (controller-based
Wi-Fi such as Cisco FlexConnect local switching does this): clients only ever
receive *unicast* mDNS replies to their own queries.

Proxy mode answers like a switch's service-discovery gateway instead. It
learns services on the source interfaces and registers copies of them on the
target interfaces, pointing at the real device's address. Two paths deliver
answers to clients that multicast cannot reach:

* A client's first query in a lookup asks for a unicast reply (the QU bit);
  python-zeroconf, which publishes the copies, honours that.
* Later refresh queries ask for multicast replies (QM), which such Wi-Fi drops.
  A small responder answers those with unicast too, so browse lists stay put.

Decision logic lives in plain functions near the top of this file so it can be
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
import time
from dataclasses import dataclass, field

log = logging.getLogger("mdns-proxy")

# Linux socket option: when 1 (the default) a socket bound to the mDNS port
# receives multicast for every group joined anywhere on the host, whatever
# interface it arrived on. Not exposed by Python's socket module.
IP_MULTICAST_ALL = 49

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
BASE = "base"
DEVICE_INFO = "_device-info._tcp.local."
SERVICE_TYPES = "_services._dns-sd._udp.local."

# TXT keys that carry a hardware model: AirPlay/_device-info use "model",
# RAOP uses "am", companion-link uses "rpMd".
MODEL_KEYS = ("model", "am", "rpmd")


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


def parse_service_spec(spec: str) -> tuple[str, list[str]]:
    """Parse a service entry in dns-sd's notation.

    '_ipp._tcp,_universal' -> ('_ipp._tcp.local.', ['_universal'])
    '_airplay._tcp.local.' -> ('_airplay._tcp.local.', [])
    """
    parts = [p.strip() for p in spec.split(",")]
    return normalize_type(parts[0]), [p for p in parts[1:] if p]


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


def browse_types(specs: list[str], global_subtypes: list[str]) -> list[str]:
    """Every type to browse: each service, its own subtypes, then any global ones."""
    out: list[str] = []
    for spec in specs:
        base, own = parse_service_spec(spec)
        out.append(base)
        for sub in own + [s for s in global_subtypes if s not in own]:
            out.append(variant_type(base, sub))
    return out


def instance_label(name: str, base_type: str) -> str:
    """'Living Room._airplay._tcp.local.' -> 'Living Room' (the device's display name)."""
    if name.lower().endswith("." + base_type.lower()):
        return name[: -len(base_type) - 1]
    return name


def name_excluded(name: str, server: str | None, patterns: list[str]) -> bool:
    """Case-insensitive substring match against the instance name or host name."""
    haystacks = [name.lower(), (server or "").lower()]
    return any(p.lower() in h for p in patterns if p for h in haystacks)


def txt_models(properties: dict | None) -> list[str]:
    """Hardware model strings from a TXT record (keys compared case-insensitively)."""
    out: list[str] = []
    for key, value in (properties or {}).items():
        k = key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
        if k.lower() in MODEL_KEYS and value:
            out.append(value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value))
    return out


def roaming_model(models: list[str], prefixes: list[str]) -> str | None:
    """The first model that marks a device which moves between networks, if any.

    Laptops, phones and tablets hop between VLANs (docked, then on Wi-Fi). A
    copy of their records left behind on another VLAN makes them see their own
    name claimed and rename themselves, so they are never proxied.
    """
    for m in models:
        if any(p and m.lower().startswith(p.lower()) for p in prefixes):
            return m
    return None


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


def select_answers(questions, known_answers, infos) -> tuple[list, list]:
    """Records answering `questions` from the published ServiceInfos.

    Returns (answers, additionals). Records the querier already holds with at
    least half their TTL left are dropped (RFC 6762 known-answer suppression).
    """
    from zeroconf import const

    ptr, srv, txt, a, any_ = (const._TYPE_PTR, const._TYPE_SRV, const._TYPE_TXT,
                              const._TYPE_A, const._TYPE_ANY)
    answers: list = []
    extras: list = []

    for q in questions:
        qname, qtype = q.name.lower(), q.type
        for info in infos:
            name = info.name.lower()
            if qtype in (ptr, any_) and qname == info.type.lower():
                answers.append(info.dns_pointer())
                extras += [info.dns_service(), info.dns_text(), *info.dns_addresses()]
            if qtype in (srv, any_) and qname == name:
                answers.append(info.dns_service())
                extras += info.dns_addresses()
            if qtype in (txt, any_) and qname == name:
                answers.append(info.dns_text())
            if qtype in (a, any_) and info.server and qname == info.server.lower():
                answers += info.dns_addresses()

    def keep(records, exclude=()):
        out: list = []
        for r in records:
            if r in out or r in exclude:
                continue
            if any(k == r and k.ttl >= r.ttl / 2 for k in known_answers):
                continue
            out.append(r)
        return out

    kept = keep(answers)
    return kept, keep(extras, exclude=kept)


# --------------------------------------------------------------------------- #
# Host introspection and sockets                                              #
# --------------------------------------------------------------------------- #

def iface_from_host(name: str) -> Iface:
    out = subprocess.run(
        ["ip", "-j", "-4", "addr", "show", "dev", name],
        check=True, capture_output=True, text=True,
    ).stdout
    for entry in json.loads(out or "[]"):
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
        return sock

    zc_net.new_listen_socket = listen_socket_joined_groups_only


def open_responder_socket(iface_ip: str) -> socket.socket:
    """UDP 5353 socket that hears multicast mDNS queries on one interface only.

    Replies must leave from port 5353 (RFC 6762 s.6), so it shares the mDNS
    port with the host's other responders. It binds the group address, not the
    wildcard: SO_REUSEPORT load-balances *unicast* datagrams across wildcard
    sockets, and this socket would otherwise swallow unicast replies meant for
    Home Assistant's own zeroconf. Linux still picks a unicast source address
    for what it sends, from the route to the client.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind((MDNS_ADDR, MDNS_PORT))
    if sys.platform.startswith("linux"):
        s.setsockopt(socket.IPPROTO_IP, IP_MULTICAST_ALL, 0)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(MDNS_ADDR) + socket.inet_aton(iface_ip))
    # RFC 6762 s.11: mDNS responses, unicast included, are sent with TTL 255.
    s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 255)
    s.setblocking(False)
    return s


# --------------------------------------------------------------------------- #
# Unicast responder for refresh (QM) queries                                  #
# --------------------------------------------------------------------------- #

class UnicastResponder(asyncio.DatagramProtocol):
    """Answer QM queries from clients on one target interface by unicast.

    Only questions without the QU bit are handled; python-zeroconf already
    answers QU questions by unicast, and answering twice gains nothing.
    """

    RATE_LIMIT = 1.0  # seconds between identical answers to one client

    def __init__(self, proxy: "Proxy", target: Iface):
        self.proxy = proxy
        self.target = target
        self.transport = None
        self.last_sent: dict[tuple, float] = {}

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self._handle(data, addr)
        except Exception:
            log.debug("Responder failed on a packet from %s", addr, exc_info=True)

    def _handle(self, data: bytes, addr) -> None:
        from zeroconf import DNSIncoming, DNSOutgoing, const

        ip, port = addr[0], addr[1]
        # Legacy unicast queries (source port not 5353) need the query ID
        # echoed; python-zeroconf answers those correctly already.
        if port != MDNS_PORT:
            return
        if ip in self.proxy.own_ips or not self.target.contains(ip):
            return
        msg = DNSIncoming(data, (ip, port))
        if not msg.valid or not msg.is_query():
            return
        questions = [q for q in msg.questions if not q.unicast]
        if not questions:
            return

        infos = self.proxy.published_infos(self.target.name)
        if not infos:
            return
        answers, extras = select_answers(questions, msg.answers(), infos)
        if not answers:
            return

        now = time.monotonic()
        key = (ip, port, tuple(sorted((q.name.lower(), q.type) for q in questions)))
        if now - self.last_sent.get(key, 0.0) < self.RATE_LIMIT:
            return
        self.last_sent[key] = now
        if len(self.last_sent) > 4096:
            cutoff = now - 60
            self.last_sent = {k: t for k, t in self.last_sent.items() if t > cutoff}

        out = DNSOutgoing(const._FLAGS_QR_RESPONSE | const._FLAGS_AA, multicast=False)
        for r in answers:
            out.add_answer_at_time(r, 0)  # 0: records are fresh, skip the expiry check
        for r in extras:
            out.add_additional_answer(r)
        for packet in out.packets():
            self.transport.sendto(packet, (ip, port))
        log.debug("Answered %s from %s with %d record(s)",
                  ", ".join(q.name for q in questions), ip, len(answers) + len(extras))


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
    def __init__(self, sources, targets, services, subtypes, own_ips,
                 exclude_sources, exclude_names, skip_models):
        self.sources: list[Iface] = sources
        self.targets: list[Iface] = targets
        self.specs: list[str] = list(services)
        self.proxied_bases = {parse_service_spec(s)[0].lower() for s in self.specs}
        self.global_subtypes = list(subtypes)
        self.own_ips: set[str] = own_ips
        self.exclude_sources: set[str] = set(exclude_sources)
        self.exclude_names: list[str] = list(exclude_names)
        self.skip_models: list[str] = list(skip_models)

        self.learner = None
        self.browser = None
        self.publishers: dict[tuple[str, str], object] = {}
        self.responders: list = []
        self.learned: dict[str, Entry] = {}
        # (target name, label, instance key) -> (ServiceInfo, fingerprint)
        self.published: dict[tuple[str, str, str], tuple[object, tuple]] = {}
        self.skipped: dict[str, str] = {}
        self.roaming_labels: dict[str, str] = {}   # display name -> model
        self.seen_types: set[str] = set()
        self.queue: asyncio.Queue = asyncio.Queue()

    # -- lifecycle ----------------------------------------------------------- #

    async def start(self) -> None:
        from zeroconf import DNSQuestionType, IPVersion
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

        self.learner = AsyncZeroconf(
            interfaces=[s.ip for s in self.sources], ip_version=IPVersion.V4Only,
        )
        labels = {BASE}
        for t in browse_types(self.specs, self.global_subtypes):
            labels.add(split_type(t)[0])
        for target in self.targets:
            for label in sorted(labels):
                self.publishers[(target.name, label)] = AsyncZeroconf(
                    interfaces=[target.ip], ip_version=IPVersion.V4Only,
                )

        loop = asyncio.get_running_loop()
        for target in self.targets:
            transport, _ = await loop.create_datagram_endpoint(
                lambda t=target: UnicastResponder(self, t),
                sock=open_responder_socket(target.ip),
            )
            self.responders.append(transport)

        types = browse_types(self.specs, self.global_subtypes)
        log.info("Learning on %s: %s", ", ".join(s.name for s in self.sources), " ".join(types))
        log.info("Publishing on %s (with unicast answers to refresh queries)",
                 ", ".join(t.name for t in self.targets))
        # QM, not QU: a unicast reply to the learner would be load-balanced
        # across every socket bound to UDP 5353 on this host (Home Assistant's
        # zeroconf included) and could land in the wrong one.
        self.browser = AsyncServiceBrowser(
            self.learner.zeroconf, types + [DEVICE_INFO, SERVICE_TYPES],
            handlers=[self._on_change], question_type=DNSQuestionType.QM,
        )

    async def stop(self) -> None:
        log.info("Stopping; withdrawing %d published records", len(self.published))
        if self.browser is not None:
            await self.browser.async_cancel()
        for transport in self.responders:
            transport.close()
        for key in list(self.published):
            await self._unpublish(*key)
        for pub in self.publishers.values():
            await pub.async_close()
        if self.learner is not None:
            await self.learner.async_close()

    def published_infos(self, target: str) -> list:
        return [info for (t, _, _), (info, _) in self.published.items() if t == target]

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

    async def report(self, first: float = 90, every: float = 1800) -> None:
        await asyncio.sleep(first)
        while True:
            names = sorted({e.name for e in self.learned.values() if e.targets})
            log.info("Proxying %d service(s): %s", len(names), "; ".join(names) or "none")
            proxied = sorted(t for t in self.seen_types if t in self.proxied_bases)
            other = sorted(t for t in self.seen_types if t not in self.proxied_bases)
            log.info("Service types on source VLANs - proxied: %s", " ".join(proxied) or "none")
            log.info("Service types on source VLANs - not proxied: %s", " ".join(other) or "none")
            if self.roaming_labels:
                log.info("Never proxied (laptops/phones): %s", "; ".join(
                    f"{n} ({m})" for n, m in sorted(self.roaming_labels.items())))
            await asyncio.sleep(every)

    # -- event handling ------------------------------------------------------ #

    async def _handle(self, browsed_type: str, name: str, state_change) -> None:
        from zeroconf import DNSQuestionType, IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceInfo

        if browsed_type == SERVICE_TYPES:
            if state_change is not ServiceStateChange.Removed:
                self.seen_types.add(name.lower())
            return

        removed = state_change is ServiceStateChange.Removed

        if browsed_type == DEVICE_INFO:
            if removed:
                return
            info = AsyncServiceInfo(DEVICE_INFO, name)
            if not await info.async_request(self.learner.zeroconf, 3000,
                                            question_type=DNSQuestionType.QM):
                return
            model = roaming_model(txt_models(info.properties), self.skip_models)
            if model:
                label = instance_label(name, DEVICE_INFO)
                if label not in self.roaming_labels:
                    log.info("Will not proxy %s: roaming device (model %s)", label, model)
                self.roaming_labels[label] = model
                for key, entry in list(self.learned.items()):
                    if instance_label(entry.name, entry.base_type) == label:
                        await self._sync(entry, withdraw_all=True)
                        del self.learned[key]
            return

        label, base = split_type(browsed_type)
        key = name.lower()

        if removed:
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
        if not await info.async_request(self.learner.zeroconf, 3000,
                                        question_type=DNSQuestionType.QM):
            log.debug("Could not resolve %s", name)
            return

        addresses = info.parsed_addresses(IPVersion.V4Only)
        display = instance_label(name, base)
        model = roaming_model(txt_models(info.properties), self.skip_models)
        if model or display in self.roaming_labels:
            reason = f"roaming device (model {model or self.roaming_labels[display]})"
            targets = []
        elif name_excluded(name, info.server, self.exclude_names):
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
            if (t.name, label) in self.publishers
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
        suffix = "" if label == BASE else f" (subtype {label})"
        if existing is None:
            # cooperating_responders: the real device legitimately owns this
            # name on its own VLAN, so do not probe for or fight conflicts.
            await (await publisher.async_register_service(info, cooperating_responders=True))
            log.info("Published %s on %s%s", entry.name, target, suffix)
        else:
            await (await publisher.async_update_service(info))
            log.info("Updated %s on %s%s", entry.name, target, suffix)
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
        skip_models=options.get("proxy_skip_models") or [],
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
