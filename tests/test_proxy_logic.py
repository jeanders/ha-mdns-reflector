"""Tests for the network-free decision logic in proxy mode.

Run from the repository root:  python3 -m unittest discover -s tests
"""

import ipaddress
import pathlib
import sys
import unittest

try:
    import zeroconf
except ImportError:  # the pure helpers are still tested without it
    zeroconf = None

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

    def test_service_spec_uses_dns_sd_notation(self):
        self.assertEqual(p.parse_service_spec("_ipp._tcp,_universal"),
                         ("_ipp._tcp.local.", ["_universal"]))
        self.assertEqual(p.parse_service_spec(" _airplay._tcp.local. "),
                         ("_airplay._tcp.local.", []))

    def test_per_service_subtypes_apply_only_to_their_service(self):
        self.assertEqual(
            p.browse_types(["_ipp._tcp,_universal", "_airplay._tcp"], []),
            ["_ipp._tcp.local.", "_universal._sub._ipp._tcp.local.", "_airplay._tcp.local."],
        )

    def test_global_subtype_is_not_duplicated(self):
        self.assertEqual(
            p.browse_types(["_ipp._tcp,_universal"], ["_universal"]),
            ["_ipp._tcp.local.", "_universal._sub._ipp._tcp.local."],
        )

    def test_device_label_strips_raop_prefix(self):
        self.assertEqual(p.device_label("8A6897F05564@John’s MacBook Pro (2767)._raop._tcp.local.",
                                        "_raop._tcp.local."), "John’s MacBook Pro (2767)")
        self.assertEqual(p.device_label("000678de9f34Denon AVR-X1800H._spotify-connect._tcp.local.",
                                        "_spotify-connect._tcp.local."),
                         "000678de9f34Denon AVR-X1800H")

    def test_instance_label(self):
        self.assertEqual(p.instance_label("Living Room._airplay._tcp.local.", "_airplay._tcp.local."),
                         "Living Room")
        self.assertEqual(p.instance_label("Odd.name", "_airplay._tcp.local."), "Odd.name")


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


class RoamingDevices(unittest.TestCase):
    PREFIXES = ["Mac", "iMac", "iPhone", "iPad", "iPod"]

    def test_model_keys_from_each_apple_service(self):
        self.assertEqual(p.txt_models({b"model": b"Mac16,7", b"deviceid": b"x"}), ["Mac16,7"])
        self.assertEqual(p.txt_models({b"am": b"AppleTV6,2"}), ["AppleTV6,2"])
        self.assertEqual(p.txt_models({b"rpMd": b"iPhone14,2"}), ["iPhone14,2"])
        self.assertEqual(p.txt_models({b"model": None, b"ty": b"HP"}), [])
        self.assertEqual(p.txt_models(None), [])

    def test_laptops_and_phones_roam(self):
        for model in ("MacBookPro18,1", "Mac16,7", "Macmini9,1", "iMac21,1", "iPhone14,2", "iPad13,4"):
            self.assertEqual(p.roaming_model([model], self.PREFIXES), model)

    def test_fixed_devices_do_not(self):
        for model in ("AppleTV6,2", "AudioAccessory5,1", "AVR-X1800H", "HP LaserJet"):
            self.assertIsNone(p.roaming_model([model], self.PREFIXES))

    def test_empty_prefix_list_disables_the_check(self):
        self.assertIsNone(p.roaming_model(["MacBookPro18,1"], []))
        self.assertIsNone(p.roaming_model(["MacBookPro18,1"], [""]))


class RoamingWithdrawal(unittest.IsolatedAsyncioTestCase):
    async def test_services_learned_before_the_model_was_known_are_withdrawn(self):
        proxy = p.Proxy([VLAN10], [VLAN20], [], [], OWN, [], [], ["Mac"])
        withdrawn = []

        async def fake_sync(entry, withdraw_all=False):
            withdrawn.append(entry.name)
        proxy._sync = fake_sync

        mac = "JohnAndersonSHI-3365.local."
        for name, base, server in [
            ("John’s MacBook Pro (2767)._companion-link._tcp.local.", "_companion-link._tcp.local.", mac),
            ("Office._ipp._tcp.local.", "_ipp._tcp.local.", mac),  # shared printer: same host
            ("Living Room._companion-link._tcp.local.", "_companion-link._tcp.local.", "Living-Room-2.local."),
        ]:
            proxy.learned[name.lower()] = p.Entry(base_type=base, name=name, server=server)

        await proxy._mark_roaming("John’s MacBook Pro (2767)", mac.upper(), "Mac16,7")

        self.assertEqual(sorted(withdrawn), ["John’s MacBook Pro (2767)._companion-link._tcp.local.",
                                             "Office._ipp._tcp.local."])
        self.assertEqual(list(proxy.learned), ["living room._companion-link._tcp.local."])
        self.assertEqual(proxy.roaming_hosts, {mac.lower(): "Mac16,7"})


@unittest.skipIf(zeroconf is None, "python-zeroconf not installed")
class UnicastAnswers(unittest.TestCase):
    def setUp(self):
        import socket
        from zeroconf import ServiceInfo, const
        self.const = const
        self.info = ServiceInfo(
            "_airplay._tcp.local.", "Living Room._airplay._tcp.local.",
            addresses=[socket.inet_aton("192.168.30.35")], port=7000,
            properties={"model": "AppleTV6,2"}, server="Living-Room-2.local.",
        )
        self.sub = ServiceInfo(
            "_universal._sub._ipp._tcp.local.", "HP LaserJet CP1025nw._ipp._tcp.local.",
            addresses=[socket.inet_aton(PRINTER)], port=631,
            properties={"ty": "HP"}, server="HomePrinter.local.",
        )

    def q(self, name, qtype):
        from zeroconf import DNSQuestion
        return DNSQuestion(name, qtype, self.const._CLASS_IN)

    def test_browse_answers_ptr_with_srv_txt_a_additionals(self):
        answers, extras = p.select_answers([self.q("_airplay._tcp.local.", self.const._TYPE_PTR)],
                                           [], [self.info, self.sub])
        self.assertEqual(answers, [self.info.dns_pointer()])
        self.assertEqual(set(extras), {self.info.dns_service(), self.info.dns_text(),
                                       *self.info.dns_addresses()})

    def test_subtype_browse(self):
        answers, _ = p.select_answers(
            [self.q("_universal._sub._ipp._tcp.local.", self.const._TYPE_PTR)], [], [self.info, self.sub])
        self.assertEqual(answers, [self.sub.dns_pointer()])

    def test_resolve_and_address(self):
        answers, _ = p.select_answers(
            [self.q("living room._airplay._tcp.local.", self.const._TYPE_SRV),
             self.q("Living-Room-2.local.", self.const._TYPE_A)], [], [self.info])
        self.assertEqual(answers, [self.info.dns_service(), *self.info.dns_addresses()])

    def test_known_answer_suppression(self):
        ptr = self.info.dns_pointer()
        answers, _ = p.select_answers([self.q("_airplay._tcp.local.", self.const._TYPE_PTR)],
                                      [ptr], [self.info])
        self.assertEqual(answers, [])

    def test_unrelated_question(self):
        self.assertEqual(p.select_answers([self.q("_hap._tcp.local.", self.const._TYPE_PTR)],
                                          [], [self.info]), ([], []))

    def test_responder_replies_by_unicast_to_qm_browse(self):
        from zeroconf import DNSIncoming, DNSOutgoing

        class FakeProxy:
            own_ips = OWN
            def published_infos(inner, target):
                return [self.info]

        class FakeTransport:
            sent = []
            def sendto(inner, data, addr):
                FakeTransport.sent.append((data, addr))

        responder = p.UnicastResponder(FakeProxy(), VLAN20)
        responder.connection_made(FakeTransport())
        query = DNSOutgoing(self.const._FLAGS_QR_QUERY)
        query.add_question(self.q("_airplay._tcp.local.", self.const._TYPE_PTR))
        packet = query.packets()[0]

        responder.datagram_received(packet, ("192.168.20.10", 5353))
        responder.datagram_received(packet, ("192.168.20.10", 5353))  # rate limited
        responder.datagram_received(packet, ("192.168.10.26", 5353))  # not on the target VLAN
        responder.datagram_received(packet, ("192.168.20.11", 49152))  # legacy unicast
        self.assertEqual(len(FakeTransport.sent), 1)
        data, addr = FakeTransport.sent[0]
        self.assertEqual(addr, ("192.168.20.10", 5353))
        reply = DNSIncoming(data)
        self.assertFalse(reply.is_query())
        self.assertIn(self.info.dns_pointer(), reply.answers())


if __name__ == "__main__":
    unittest.main()
