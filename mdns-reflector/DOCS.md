# mDNS Reflector

Relays mDNS/Bonjour announcements between VLAN interfaces on the Home Assistant
host, so HomeKit, AirPlay and ONVIF devices on one VLAN can be discovered from
another. Runs Avahi in reflector mode with per-service filtering.

## Before you install: this may not be the piece you need

The reflector relays **other devices'** services. It does nothing for Home
Assistant's or Scrypted's own accessories — those are advertised by the Pi
itself.

If the goal is "my phone on VLAN 20 should see HomeKit accessories hosted by
Home Assistant or Scrypted", the simpler and far more reliable fix is to give
the Pi an address on VLAN 20 and let it advertise natively. No reflection, no
cache, no TTL games:

```sh
ha network vlan end0 20 --ipv4-method static --ipv4-address 192.168.20.5/24
```

Install this add-on for the cases multihoming can't cover: a Lutron bridge, an
Apple TV, a printer or a camera that lives on a different VLAN and must be
discovered from another one.

## Prerequisites

1. **The VLAN sub-interfaces must already exist on the host.** Create them with
   `ha network vlan <iface> <vlan-id> ...` from the SSH & Web Terminal add-on.
   Do **not** give the secondary interfaces a gateway — one default route per
   host.
2. **The switch port must be a trunk** carrying every VLAN involved. Keep the
   management VLAN untagged/native so the Pi stays reachable if tagging is
   wrong.
3. **Turn off any other reflector for the same VLANs** — a switch's mDNS
   gateway / service-discovery gateway, or a firewall's Avahi package. Two
   reflectors re-announcing each other's records cause records to appear and
   vanish on a few-second cycle, which looks exactly like the problem you were
   trying to fix.

## Installation

**From the repository (normal route):**

1. **Settings → Add-ons → Add-on Store**, then ⋮ → **Repositories**, and add
   `https://github.com/jeanders/ha-mdns-reflector`.
2. Install **mDNS Reflector** from the list.
3. Set the options (below), then **Start**, and read the **Log** tab.

**From a local copy (for development):** copy the `mdns-reflector` folder into
the `/addons` share via the Samba or SSH add-on, then ⋮ → **Check for updates**
and install it under **Local add-ons**.

Until a release has published images, installing builds the image on your own
hardware. That takes a few minutes and pulls a Debian base image.

## What is never reflected

Avahi refuses to reflect traffic that originates from the host it runs on. From
`avahi-core/server.c`:

```c
/* We don't want to reflect local traffic, so we check if this packet
is generated locally. */
if (s->config.enable_reflector)
    from_local_iface = originates_from_local_iface(s, iface, src_address, port);
```

So the Pi's own services — Home Assistant's HomeKit bridge, Scrypted's
accessories, the host's `.local` name — are excluded from reflection whether or
not you set any filters. Those reach other VLANs by the Pi having an address
there, not by being relayed. This add-on handles everything *else* on the wire.

## Options

| Option | Type | Meaning |
|---|---|---|
| `interfaces` | list | Host interfaces to reflect between. At least two. Names must match the host exactly (`end0`, `end0.20`, `end0.30`). |
| `reflect_filters` | list | Service types allowed to cross. Empty reflects everything. The shipped default is a curated list — see [Dual-homed hosts](#dual-homed-hosts-and-endless-renaming). Matching is a substring test, so `_hap._tcp` also matches `_hap._tcp.local`. |
| `exclude_sources` | list | Source IPs whose mDNS is dropped before Avahi sees it. Empty by default. See [Dual-homed hosts](#dual-homed-hosts-and-endless-renaming). |
| `reflect_ipv6` | bool | Reflect over IPv6 as well. |
| `reflect_between_ipv4_ipv6` | bool | Bridge IPv4 and IPv6 mDNS. Off unless you know you want it. |
| `disallow_other_stacks` | bool | Leave `false`. `true` stops Avahi sharing UDP 5353 with Home Assistant's own zeroconf stack. |
| `allow_point_to_point` | bool | Include PPP/VPN-type interfaces. |
| `log_level` | enum | `debug` passes `--debug` to Avahi; anything else is normal logging. |

### Service types worth knowing

| Service | Used by |
|---|---|
| `_hap._tcp.local` | HomeKit accessories and bridges |
| `_airplay._tcp.local`, `_raop._tcp.local` | AirPlay video / audio |
| `_companion-link._tcp.local` | Apple TV & HomePod (home hub pairing) |
| `_sleep-proxy._udp.local` | Bonjour sleep proxy (Apple hubs) |
| `_googlecast._tcp.local` | Chromecast / Google TV |
| `_ipp._tcp.local`, `_printer._tcp.local` | Printers |
| `_onvif._tcp.local`, `_rtsp._tcp.local` | IP cameras |

## Dual-homed hosts and endless renaming

If a machine sits on **two reflected VLANs at once** — a laptop docked to wired
VLAN A while its Wi-Fi is on VLAN B — reflection makes it fight itself. Its
announcement on one VLAN is copied onto the other, where it is still listening.
It sees its own name claimed by what looks like a different machine, and
renames itself. Forever.

The damage is visible on the machine. On a Mac:

```sh
scutil --get ComputerName    # John's MacBook Pro (984)
scutil --get LocalHostName   # Johns-MacBook-Pro-687
```

Those numbers are rename counts, and they are two *separate* conflicts:

| What renamed | Driven by | Fixable with `reflect_filters`? |
|---|---|---|
| ComputerName, service instances | PTR / SRV / TXT | **Yes** |
| LocalHostName, the `.local` name | A / AAAA | **No** |

Avahi's filter only inspects PTR, SRV and TXT records; A and AAAA bypass it
entirely. That is why this add-on offers two separate mechanisms.

### 1. The default filter list (handles the service half)

The shipped `reflect_filters` omits Apple's peer-to-peer and identity services
— `_companion-link`, `_rdlink`, `_device-info`, `_sleep-proxy`, `_airplay`,
`_raop` — which are the ones dual-homed Macs advertise about themselves. This
stops the ComputerName churn without affecting HomeKit, Matter, Chromecast or
printing.

The cost: **AirPlay is not reflected by default.** If you need to AirPlay
across VLANs, add `_airplay._tcp.local` and `_raop._tcp.local` back, and expect
the rename behaviour to return on any dual-homed Mac.

### 2. `exclude_sources` (handles the hostname half)

List **both** addresses of the dual-homed machine:

```yaml
exclude_sources:
  - 192.168.10.5     # the Mac's wired address
  - 192.168.20.10    # the same Mac's Wi-Fi address
```

Its mDNS is then dropped before Avahi sees it, so nothing of that host is ever
reflected and it has nothing to argue with.

Three things to know before enabling it:

- It installs **iptables rules in the host's network namespace**, in a
  dedicated `MDNS_REFLECTOR` chain. The chain is rebuilt on every start and
  removed when the service stops, but a force-killed container can leave it
  behind. To clear it by hand: `iptables -D INPUT -p udp --dport 5353 -j
  MDNS_REFLECTOR; iptables -F MDNS_REFLECTOR; iptables -X MDNS_REFLECTOR`.
- Excluded hosts become invisible to **Home Assistant's own discovery** too,
  not just to reflection. For a laptop that is usually fine; do not do it to a
  device you want Home Assistant to find.
- It is per-IP, so DHCP reassignment breaks it. Give those machines static
  leases.

### 3. The fix that needs no software

Stop the machine being on two reflected VLANs. Put the dock's wired port on the
same VLAN as its Wi-Fi, or turn Wi-Fi off when docked. This removes the cause
rather than suppressing the symptom, and it is the only option with no
trade-off.

## Suggested configuration

Three VLANs, with the default curated filter list:

```yaml
interfaces:
  - end0        # VLAN 10 - Home Assistant / Scrypted
  - end0.20     # VLAN 20 - phones, Macs
  - end0.30     # VLAN 30 - cameras
reflect_filters:
  - _hap._tcp.local
  - _matter._tcp.local
  - _matterc._udp.local
  - _esphomelib._tcp.local
  - _googlecast._tcp.local
  - _spotify-connect._tcp.local
  - _ipp._tcp.local
  - _ipps._tcp.local
  - _printer._tcp.local
  - _pdl-datastream._tcp.local
exclude_sources: []
reflect_ipv6: false
reflect_between_ipv4_ipv6: false
disallow_other_stacks: false
allow_point_to_point: false
log_level: info
```

To reflect everything instead, set `reflect_filters: []` — and read
[Dual-homed hosts](#dual-homed-hosts-and-endless-renaming) first, because
that is what brings the renaming behaviour back.

**One thing to keep in mind about the camera VLAN.** Reflecting everything into
and out of VLAN 30 carries camera announcements across the boundary that VLAN
exists to create. It's a reasonable default for getting things working; it's
worth revisiting once they are.

## Verifying it works

From a machine on the far VLAN:

```sh
dns-sd -B _hap._tcp local          # do the services appear?
dns-sd -L "Some Accessory" _hap._tcp local   # does it resolve to host:port?
```

Browsing succeeds far more readily than resolving. A service that lists but
will not resolve is the signature of a half-working reflector, so test both.
Run the resolve several times — the failure is usually intermittent, not total.

## Troubleshooting

**Add-on won't start, log mentions AppArmor.** Add `apparmor: false` to
`config.yaml` and reinstall.

**"Failed to create server object" or a port 5353 complaint.** Something else
holds the port with exclusive intent. Confirm `disallow_other_stacks` is
`false`.

**Interfaces reported missing.** The add-on refuses to start rather than
silently reflect nothing. Check `ip -o link show` on the host — VLAN interfaces
disappear if the switch port isn't trunking the tag.

**Services appear then vanish every few seconds.** Two reflectors are fighting.
Disable mDNS reflection on the switch or firewall.

**Nothing crosses at all.** Verify the sub-interface actually has an IPv4
address — Avahi ignores an interface without one, and the log says so at start.
