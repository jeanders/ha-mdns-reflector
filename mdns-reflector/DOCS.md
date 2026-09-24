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
   `ha network vlan <iface> <vlan-id> ...` from the SSH & Web Terminal app.
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

1. **Settings → Apps** (called **Add-ons** before Home Assistant 2026.9), then
   ⋮ → **Repositories**, and add
   `https://github.com/jeanders/ha-mdns-reflector`.
2. Install **mDNS Reflector** from the list.
3. Set the options (below), then **Start**, and read the **Log** tab.

**From a local copy (for development):** copy the `mdns-reflector` folder into
the `/addons` share via the Samba or SSH app, then ⋮ → **Check for updates**
and install it under **Local**.

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

### 1. The default filter list (handles part of the service half)

The shipped `reflect_filters` omits `_companion-link`, `_rdlink`,
`_device-info` and `_sleep-proxy` — Apple's peer-to-peer and identity
services. They carry no cross-VLAN discovery value and are a conflict source.

`_airplay._tcp` and `_raop._tcp` **are** reflected by default, because
cross-VLAN AirPlay is usually the reason people run a reflector. They are also
a conflict source for a dual-homed machine, which is what the next section is
for.

### 1b. Allowing individual devices, not just service types

Filter entries are matched with `strstr` against the **service instance name**
(`record->data.ptr.name` for PTR, `record->key->name` for SRV and TXT), not
against the service type. So an entry can name one device:

```yaml
reflect_filters:
  - Living Room._airplay._tcp.local   # this Apple TV's AirPlay only
  - Living Room                       # or everything that device advertises
  - _hap._tcp.local                   # plus all HomeKit, from anything
```

That is how you reflect an Apple TV's AirPlay while *not* reflecting a Mac's,
even though both advertise the identical service types.

Three caveats before relying on it:

- **It does not stop hostname renaming.** A and AAAA records are reflected no
  matter what the filter says, so a dual-homed machine keeps fighting over its
  `.local` name. Only `exclude_sources` addresses that.
- **Instance names drift.** A device that has been renamed, or that has lost a
  conflict, is `Living Room (2)`. Match on a stable substring, and re-check
  after renaming anything.
- **Substrings over-match.** A filter of `Apple` matches every instance name
  containing it. Be as specific as you can stand.

A simpler option for Macs specifically: turn off **System Settings → General →
AirDrop & Handoff → AirPlay Receiver**. The Mac then stops advertising
`_airplay._tcp` and `_raop._tcp` altogether, so there is nothing to filter and
nothing to conflict over, and it can still AirPlay *to* other devices.

### 2. `exclude_sources` (handles the hostname half, and AirPlay)

List **both** addresses of the dual-homed machine:

```yaml
exclude_sources:
  - 192.168.10.5     # the Mac's wired address
  - 192.168.20.10    # the same Mac's Wi-Fi address
exclude_mode: advertisements
```

`exclude_mode` decides what gets dropped, and the distinction matters if that
machine needs to *use* cross-VLAN discovery:

| Mode | Drops | The excluded machine can still |
|---|---|---|
| `advertisements` (default) | its mDNS **responses** only | send queries, so it keeps discovering everything across VLANs |
| `all` | every mDNS packet from it | nothing across VLANs |

**For AirPlaying from a dual-homed Mac to an Apple TV on another VLAN, use
`advertisements`.** Sending to an Apple TV only needs the Mac to *hear* the
Apple TV's advertisements; the Mac's own advertisements never need to cross.
Dropping just its responses keeps discovery working while removing the records
it was arguing with.

`advertisements` mode matches the DNS QR bit with iptables' `u32` module. If
the kernel lacks it, the add-on logs a warning and falls back to dropping
everything from that host — so check the log after enabling it.

Three more things to know:

- It installs **iptables rules in the host's network namespace**, in a
  dedicated `MDNS_REFLECTOR` chain. The chain is rebuilt on every start and
  removed when the service stops, but a force-killed container can leave it
  behind. To clear it by hand: `iptables -D INPUT -p udp --dport 5353 -j
  MDNS_REFLECTOR; iptables -F MDNS_REFLECTOR; iptables -X MDNS_REFLECTOR`.
- In `all` mode, excluded hosts become invisible to **Home Assistant's own
  discovery** too. In `advertisements` mode Home Assistant will not see their
  services either, since those are exactly the packets being dropped.
- It is per-IP, so DHCP reassignment breaks it. Give those machines static
  leases.

### 2b. `exclude_names` — excluding by name instead of address

`exclude_sources` is per-IP, which DHCP breaks. `exclude_names` matches the raw
mDNS packet for a fixed substring instead, so it follows a host across
addresses:

```yaml
exclude_names:
  - MacBook
exclude_mode: advertisements
```

mDNS labels travel as plain ASCII, so a host's own name appears literally in
the packets it sends. Combined with `advertisements` mode, this drops a
machine's announcements while leaving its queries alone.

**These are fixed strings, not regular expressions.** The kernel's `string`
match has no regex support, and there is no regex anywhere in this path:
Avahi's own `reflect-filters` is a compiled-in `strstr` allow-list. If the
kernel lacks the `string` match the add-on logs an error and that entry is
simply inactive — check the log.

#### Why "MacBook" is not the default

It is tempting to ship `exclude_names: [Mac]` and be done. Both directions go
wrong:

- **False negatives.** Plenty of Macs are not named after themselves. A machine
  called `johns-laptop` or `studio` is missed entirely, and the symptom looks
  identical to a broken filter.
- **False positives.** Substrings do not respect device boundaries. `Mac`
  matches an Apple TV named `Mac's Room`, a printer named `MacGregor`, and any
  TXT value containing those letters. The result is a device that silently
  stops being discoverable, with nothing in the logs pointing at the cause.

Silently dropping a device because its name contains three particular letters
is exactly the kind of magic that produces an unexplainable bug six months
later. The option is here; pointing it at a name you have actually verified is
your decision to make.

A narrower alternative for Macs: turn off **AirPlay Receiver** (see above) so
there is nothing to exclude in the first place.

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
  - _airplay._tcp.local
  - _raop._tcp.local
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
exclude_mode: advertisements
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
