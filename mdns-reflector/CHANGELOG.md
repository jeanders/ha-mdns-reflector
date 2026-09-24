# Changelog

## 1.0.1 - Unreleased

### Fixed

- The add-on failed to start with a stock configuration. Avahi's config parser
  reads lines into a 128-byte buffer, and the default `reflect-filters=` line
  was 277 characters, so it was split mid-string and Avahi died with
  `Missing assignment in /etc/avahi/avahi-daemon.conf:28`. Found by running it
  on real hardware.

### Changed

- Default `reflect_filters` now uses short substring stems (`_hap.`, `_matter`,
  `_ipp`) instead of full service types, generating a 74-character line.
- Start-up now validates the generated line length and fails with an
  explanation, instead of letting Avahi fail cryptically.

## 1.0.0 - Unreleased

First release. Not yet published, and not yet run end to end on real hardware.

### Added

- Avahi in reflector mode across an arbitrary list of host interfaces.
- `reflect_filters` to restrict which service types may cross VLANs. Empty by
  default, meaning everything is reflected.
- Start-up validation: the add-on refuses to start when fewer than two
  interfaces are configured, or when a configured interface is missing from the
  host, and names the `ha network vlan` command needed to create one.
- Build-time assertion that the packaged Avahi supports `reflect-filters`.

- `exclude_sources`: drops mDNS from given source IPs before Avahi sees it,
  via a dedicated `MDNS_REFLECTOR` iptables chain that is rebuilt on start and
  torn down on stop. Intended for hosts that sit on two reflected VLANs at once.
- `exclude_names`: fixed-substring matching against mDNS packet contents via
  iptables' `string` module, to exclude a host by name rather than by address.
  Empty by default - see DOCS.md on why a "Mac" default misfires both ways.
- `exclude_mode`: `advertisements` (default) drops only those hosts' mDNS
  responses, so they keep discovering across VLANs - needed to AirPlay out from
  a dual-homed machine. `all` drops every mDNS packet from them. Response
  matching uses iptables' `u32` module and falls back to `all` with a warning
  where the kernel lacks it.

### Changed

- `reflect_filters` now ships a curated default rather than reflecting
  everything. It omits Apple's peer-to-peer and identity services
  (`_companion-link`, `_rdlink`, `_device-info`, `_sleep-proxy`). AirPlay
  (`_airplay._tcp`, `_raop._tcp`) is reflected, since cross-VLAN AirPlay is a
  primary use case; pair it with `exclude_sources` for dual-homed machines.

### Notes

- Publishing is disabled in the generated Avahi config, so the add-on relays
  other hosts' records only and cannot collide with Home Assistant's own
  zeroconf announcements.
- `disallow-other-stacks` defaults to `no` so Avahi shares UDP 5353 with Home
  Assistant's zeroconf stack.
