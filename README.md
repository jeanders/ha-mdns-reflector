# mDNS Reflector — Home Assistant Add-on Repository

A Home Assistant add-on that reflects mDNS/Bonjour traffic between VLAN
interfaces using Avahi, so HomeKit, AirPlay and ONVIF discovery work across
network segments.

## Installation

[![Open your Home Assistant instance and show the add add-on repository dialog with this repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fjeanders%2Fha-mdns-reflector)

Click the badge to add this repository to your Home Assistant in one step, or
add it manually:

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

Released. See [the changelog](./mdns-reflector/CHANGELOG.md).

## Releasing

Images are built and pushed by GitHub Actions when a **release is published**;
pushes and pull requests only validate that the image builds.

1. Bump `version` in `mdns-reflector/config.yaml` and update the changelog.
2. Publish a GitHub release whose tag matches that version (`1.1.0` or
   `v1.1.0`). The workflow fails the release if the two disagree.
3. The workflow builds amd64 and aarch64 images, pushes them to
   `ghcr.io/jeanders/mdns-reflector`, and publishes a multi-arch manifest.
   Users then pull prebuilt images instead of building on their own hardware.

`image:` in `mdns-reflector/config.yaml` must stay set: the release build reads
it to know where to push.

## License

MIT — see [LICENSE](./LICENSE).
