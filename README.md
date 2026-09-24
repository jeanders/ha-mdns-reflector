# mDNS Reflector — Home Assistant Add-on Repository

A Home Assistant add-on that reflects mDNS/Bonjour traffic between VLAN
interfaces using Avahi, so HomeKit, AirPlay and ONVIF discovery work across
network segments.

## Installation

1. In Home Assistant, go to **Settings → Apps** (**Add-ons** before 2026.9).
2. Open the ⋮ menu, choose **Repositories**, and add:

   ```
   https://github.com/jeanders/ha-mdns-reflector
   ```

3. Install **mDNS Reflector** from the list that appears.

## Add-ons in this repository

| App | Description |
|---|---|
| [mDNS Reflector](./mdns-reflector) | Avahi in reflector mode across VLAN interfaces, with per-service filtering. |

## Before you install

This add-on relays **other devices'** mDNS announcements between VLANs. Avahi
deliberately never reflects traffic originating from its own host, so Home
Assistant's and Scrypted's own HomeKit accessories are *not* what this fixes.

If the goal is for your phone on one VLAN to see accessories hosted by Home
Assistant itself, the better fix is to give the host an address on that VLAN
and let it advertise natively:

```sh
ha network vlan end0 20 --ipv4-method static --ipv4-address 192.168.20.5/24
```

Install this add-on for the devices multihoming can't cover — a Lutron bridge,
an Apple TV, a printer or a camera living on a VLAN of its own.

Full documentation is in [the add-on's docs](./mdns-reflector/DOCS.md).

## Status

**Not yet released.** See [the changelog](./mdns-reflector/CHANGELOG.md) for
what is and isn't verified.

## Releasing

Images are built and pushed by GitHub Actions when a **release is published**;
pushes and pull requests only validate that the image builds.

1. Bump `version` in `mdns-reflector/config.yaml` and update the changelog.
2. Publish a GitHub release whose tag matches that version (`1.1.0` or
   `v1.1.0`). The workflow fails the release if the two disagree.
3. After the first successful release, uncomment `image:` in
   `mdns-reflector/config.yaml` so users pull prebuilt images instead of
   building on their own hardware.

## License

MIT — see [LICENSE](./LICENSE).
