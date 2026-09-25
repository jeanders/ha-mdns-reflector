# Changelog

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
