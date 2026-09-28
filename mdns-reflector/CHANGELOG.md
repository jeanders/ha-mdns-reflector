# Changelog

## 1.2.0 - 2026-09-28

### Added

- Proxy mode answers refresh (QM) queries by unicast to the asking client, so
  browse lists on Wi-Fi that drops multicast no longer empty between lookups.
  Known-answer suppression and a per-client rate limit keep it quiet.
- Proxy mode never proxies laptops, phones or tablets: new option
  `proxy_skip_models` (default `Mac`, `iMac`, `iPhone`, `iPad`, `iPod`),
  matched against the model in AirPlay, RAOP and companion-link TXT records.
  Apple TVs and HomePods are proxied.
- Per-service subtypes in dns-sd notation: `_ipp._tcp,_universal`.
- A periodic audit in the log: what is proxied, every service type seen on
  the source VLANs (proxied and not), and devices skipped as roaming.

### Changed

- `proxy_services` now defaults to printing, scanning, AirPlay
  (`_airplay`, `_raop`, `_companion-link`), HomeKit (`_hap._tcp`),
  Chromecast and Spotify Connect. `proxy_subtypes` defaults to empty, since
  `_universal` moved onto the `_ipp`/`_ipps` entries. Existing
  configurations keep working unchanged.

## 1.1.0 - 2026-09-28

### Added

- **Proxy mode** (`mode: proxy`). Learns services on `proxy_sources` and
  answers for them on `proxy_targets` itself, the way a switch's
  service-discovery gateway does, instead of relaying multicast. Built for
  Wi-Fi that never delivers downstream multicast to clients, where the
  reflector cannot reach wireless clients at all. Starts with printers:
  `_ipp` and `_ipps`, plus the `_universal` subtype that AirPrint needs.
- Proxy mode never publishes a host onto a VLAN where it already has an
  address, so machines on two VLANs at once do not rename themselves, and it
  never proxies the add-on host's own services.

### Changed

- The exclusion firewall chain is now torn down in both modes, so switching
  from reflector to proxy mode leaves no rules behind.
- The image now includes Python and a pinned `zeroconf` for proxy mode.

### Known limitations

- On Wi-Fi that drops multicast, only a client's first query in each lookup
  gets an answer, and removals reach clients only when their cache expires.
  See DOCS.md.
- Reflector mode cannot reach clients on such networks. This was found on a
  Cisco Catalyst 9800 with FlexConnect local switching: reflected records were
  matched and relayed correctly but never arrived at any wireless client.

## 1.0.2 - 2026-09-24

First public release. Verified on a Raspberry Pi running Home Assistant OS
18.2, reflecting between three VLANs (`end0`, `end0.20`, `end0.30`).

### Added

- Avahi in reflector mode across an arbitrary list of host interfaces.
- `reflect_filters` to restrict which service types may cross VLANs. Matching
  is a substring test, so short stems cover several types at once.
- `exclude_sources`: drops mDNS from given source IPs before Avahi sees it,
  via a dedicated `MDNS_REFLECTOR` iptables chain that is rebuilt on start and
  torn down on stop. Intended for hosts that sit on two reflected VLANs at once.
- `exclude_names`: fixed-substring matching against mDNS packet contents via
  iptables' `string` module, to exclude a host by name rather than by address.
- `exclude_mode`: `advertisements` (default) drops only those hosts' mDNS
  responses, so they keep discovering across VLANs. `all` drops every mDNS
  packet from them. Response matching uses iptables' `u32` module and falls
  back to `all` with a warning where the kernel lacks it.
- Start-up validation: the add-on refuses to start when fewer than two
  interfaces are configured, when a configured interface is missing from the
  host, or when the filter list is too long for Avahi's parser.
- Build-time assertion that the packaged Avahi supports `reflect-filters`.
- Prebuilt amd64 and aarch64 images on GHCR.

### Defaults

- `reflect_filters`: `_hap.`, `_airplay.`, `_raop.`, `_matter`,
  `_googlecast.`, `_printer.`, `_ipp`. Apple's peer-to-peer and identity
  services (`_companion-link`, `_rdlink`, `_device-info`, `_sleep-proxy`) are
  left out because they make machines on two reflected VLANs rename themselves.
  Add `_companion-link.` if you want phones to find Apple home hubs locally.
- The add-on publishes nothing of its own: every `publish-*` switch is off, so
  it cannot collide with Home Assistant's zeroconf records.
- `disallow-other-stacks` is `no`, so Avahi shares UDP 5353 with Home
  Assistant's zeroconf stack.

### Fixed during pre-release testing

These affected the unpublished 1.0.0 and 1.0.1 builds and were found by running
the add-on on real hardware:

- **Would not start on a stock configuration.** Avahi reads config lines into a
  128-byte buffer (`char ln[128]` in `avahi-daemon/main.c`). The original
  default filter list produced a 277-character `reflect-filters=` line, which
  was split mid-string and failed with
  `Missing assignment in /etc/avahi/avahi-daemon.conf:28`. Defaults now use
  substring stems (74 characters) and the length is checked at start-up.
- **Reflection silently did nothing.** `disable-publishing=yes` let the daemon
  start and match records, but nothing reached the wire. It has been dropped in
  favour of the individual `publish-*` switches.
