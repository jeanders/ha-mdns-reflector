"""Tests for the network-free decision logic in proxy mode.

Run from the repository root:  python3 -m unittest discover -s tests
"""

import ipaddress
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "mdns-reflector" / "rootfs" / "opt" / "mdns-proxy"))

import mdns_proxy as p  # noqa: E402


def iface(name, cidr):
    ip = cidr.split("/")[0]
    return p.Iface(name, ip, ipaddress.IPv4Network(cidr, strict=False))


VLAN10 = iface("end0", "192.168.10.75/24")
VLAN20 = iface("end0.20", "192.168.20.104/24")
VLAN30 = iface("end0.30", "192.168.30.199/24")
OWN = {"192.168.10.75", "192.168.20.104", "192.168.30.199"}
PRINTER = "192.168.10.65"


class TypeNames(unittest.TestCase):
    def test_normalize_accepts_every_spelling(self):
        for spelling in ("_ipp._tcp", "_ipp._tcp.local", "_ipp._tcp.local.", " _ipp._tcp.local. "):
            self.assertEqual(p.normalize_type(spelling), "_ipp._tcp.local.")

    def test_split_subtype(self):
        self.assertEqual(p.split_type("_universal._sub._ipp._tcp.local."),
                         ("_universal", "_ipp._tcp.local."))

    def test_split_base(self):
        self.assertEqual(p.split_type("_ipp._tcp.local."), (p.BASE, "_ipp._tcp.local."))

    def test_variant_round_trips(self):
        for browsed in ("_ipp._tcp.local.", "_universal._sub._ipps._tcp.local."):
            self.assertEqual(p.variant_type(*reversed(p.split_type(browsed))), browsed)

    def test_browse_types_covers_services_and_subtypes(self):
        self.assertEqual(
            p.browse_types(["_ipp._tcp", "_ipps._tcp.local."], ["_universal"]),
            ["_ipp._tcp.local.", "_universal._sub._ipp._tcp.local.",
             "_ipps._tcp.local.", "_universal._sub._ipps._tcp.local."],
        )


class Planning(unittest.TestCase):
    def plan(self, addresses, sources=(VLAN10,), targets=(VLAN20,), excluded=()):
        return p.plan_targets(list(addresses), list(sources), list(targets), OWN, set(excluded))

    def test_printer_on_vlan10_is_published_on_wifi(self):
        targets, reason = self.plan([PRINTER])
        self.assertEqual(targets, [VLAN20])
        self.assertEqual(reason, "ok")

    def test_this_hosts_own_services_are_never_proxied(self):
        # Home Assistant and Scrypted advertise from the Pi; they are already
        # native on every VLAN the Pi sits on.
        targets, reason = self.plan(["192.168.10.75"])
        self.assertEqual(targets, [])
        self.assertIn("this host", reason)

    def test_dual_homed_host_is_not_published_where_it_is_native(self):
        # A laptop on wired VLAN 10 and Wi-Fi VLAN 20 at once: publishing its
        # records on VLAN 20 would make it rename itself.
        targets, reason = self.plan(["192.168.10.26", "192.168.20.33"])
        self.assertEqual(targets, [])
        self.assertIn("native", reason)

    def test_dual_homed_host_still_reaches_other_targets(self):
        targets, _ = self.plan(["192.168.10.26", "192.168.20.33"], targets=(VLAN20, VLAN30))
        self.assertEqual(targets, [VLAN30])

    def test_address_off_every_source_subnet_is_ignored(self):
        targets, reason = self.plan(["10.9.9.9"])
        self.assertEqual(targets, [])
        self.assertIn("source", reason)

    def test_no_address_is_ignored(self):
        self.assertEqual(self.plan([])[0], [])

    def test_excluded_source_address(self):
        targets, reason = self.plan([PRINTER], excluded=[PRINTER])
        self.assertEqual(targets, [])
        self.assertIn("excluded", reason)


class NameExclusion(unittest.TestCase):
    def test_matches_instance_name_case_insensitively(self):
        self.assertTrue(p.name_excluded("John's MacBook Pro._ipp._tcp.local.", None, ["macbook"]))

    def test_matches_host_name(self):
        self.assertTrue(p.name_excluded("Office._ipp._tcp.local.", "JohnAndersonSHI-3365.local.",
                                        ["AndersonSHI"]))

    def test_empty_patterns_never_match(self):
        self.assertFalse(p.name_excluded("anything._ipp._tcp.local.", "x.local.", ["", ""]))

    def test_non_matching(self):
        self.assertFalse(p.name_excluded("HP LaserJet CP1025nw._ipp._tcp.local.",
                                         "HomePrinter.local.", ["MacBook"]))


if __name__ == "__main__":
    unittest.main()
