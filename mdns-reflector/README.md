# Home Assistant Add-on: mDNS Reflector

Reflects mDNS/Bonjour announcements between VLAN interfaces using Avahi, so
HomeKit, AirPlay and ONVIF discovery work across network segments.

- Reflects every service type by default; `reflect_filters` narrows it.
- Never reflects the host's own announcements, so it cannot collide with Home
  Assistant's own zeroconf records.
- Refuses to start when a configured interface is missing, rather than
  silently reflecting nothing.

Requires VLAN sub-interfaces on the host (`ha network vlan ...`) and a trunk
port on the switch feeding it.

See [DOCS.md](./DOCS.md) for configuration, verification and troubleshooting.
