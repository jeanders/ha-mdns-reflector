# Changelog

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

### Notes

- Publishing is disabled in the generated Avahi config, so the add-on relays
  other hosts' records only and cannot collide with Home Assistant's own
  zeroconf announcements.
- `disallow-other-stacks` defaults to `no` so Avahi shares UDP 5353 with Home
  Assistant's zeroconf stack.
