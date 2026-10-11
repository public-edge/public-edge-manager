import importlib.util
import ipaddress
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dns.edns
import dns.flags
import dns.message
import dns.rcode
import dns.rdatatype
import dns.zone
from public_edge_manager import authority as public_authority


SOURCE = Path(__file__).parents[1] / "routeros"
os.environ["AUTHORITY_CONFIG"] = str(SOURCE / "authority.example.json")
spec = importlib.util.spec_from_file_location("routeros_authority", SOURCE / "authority.py")
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


class RouterOSAuthorityTests(unittest.TestCase):
    def setUp(self):
        origin = router.ZONES[0]
        zone = dns.zone.from_text(
            "@ 300 IN SOA ns1.api.example.com. hostmaster.example.com. 12 60 60 86400 30\n"
            "@ 300 IN NS ns1.api.example.com.\n"
            "@ 30 IN A 203.0.113.9\n",
            origin=origin, relativize=True,
        )
        with router.LOCK:
            router.COPIES.clear()
            router.LAST_REFRESH.clear()
            router.COPIES[origin] = zone
            router.LAST_REFRESH[origin] = time.time()

    def test_axfr_copy_answers_authoritatively_when_upstreams_fail(self):
        query = dns.message.make_query("api.example.com.", dns.rdatatype.A)
        with mock.patch.object(router.dns.query, "udp", side_effect=OSError("offline")):
            response = dns.message.from_wire(router.answer_wire(query.to_wire(), "8.8.8.8"))
        self.assertTrue(response.flags & dns.flags.AA)
        self.assertFalse(response.flags & dns.flags.RA)
        self.assertEqual(response.answer[0][0].address, "203.0.113.9")
        missing = dns.message.make_query("missing.api.example.com.", dns.rdatatype.A)
        with mock.patch.object(router.dns.query, "udp", side_effect=OSError("offline")):
            response = dns.message.from_wire(router.answer_wire(missing.to_wire(), "8.8.8.8"))
        self.assertEqual(response.rcode(), dns.rcode.NXDOMAIN)
        self.assertEqual(response.authority[0].rdtype, dns.rdatatype.SOA)

    def test_public_source_is_carried_as_ecs_without_overwriting_client_ecs(self):
        query = dns.message.make_query("api.example.com.", dns.rdatatype.A)
        router.add_client_subnet(query, "8.8.8.8")
        self.assertEqual(len(query.options), 1)
        self.assertIsInstance(query.options[0], dns.edns.ECSOption)
        router.add_client_subnet(query, "9.9.9.9")
        self.assertEqual(len(query.options), 1)

    def test_stale_copy_fails_closed(self):
        router.LAST_REFRESH[router.ZONES[0]] = 0
        query = dns.message.make_query("api.example.com.", dns.rdatatype.A)
        response = router.local_answer(query)
        self.assertEqual(response.rcode(), dns.rcode.SERVFAIL)

    def test_out_of_zone_is_refused_without_relay(self):
        query = dns.message.make_query("outside.example.org.", dns.rdatatype.A)
        with mock.patch.object(router.dns.query, "udp") as relay:
            response = dns.message.from_wire(router.answer_wire(query.to_wire(), "8.8.8.8"))
        self.assertEqual(response.rcode(), dns.rcode.REFUSED)
        relay.assert_not_called()

    def test_upstream_servfail_uses_fresh_local_copy(self):
        query = dns.message.make_query("api.example.com.", dns.rdatatype.A)
        failed = dns.message.make_response(query)
        failed.flags |= dns.flags.AA
        failed.set_rcode(dns.rcode.SERVFAIL)
        with mock.patch.object(router.dns.query, "udp", return_value=failed):
            response = dns.message.from_wire(router.answer_wire(query.to_wire(), "8.8.8.8"))
        self.assertEqual(response.rcode(), dns.rcode.NOERROR)
        self.assertEqual(response.answer[0][0].address, "203.0.113.9")

    def test_public_edge_axfr_populates_persistent_fallback(self):
        origin = router.ZONES[0]
        zone_name = origin.to_text()
        query = dns.message.make_query(zone_name, dns.rdatatype.AXFR)
        with mock.patch.object(public_authority, "AUTHORITY_ZONES", [zone_name]), \
             mock.patch.object(public_authority, "AXFR_ZONES", {zone_name}), \
             mock.patch.object(public_authority, "AXFR_ALLOWED_CIDRS", [ipaddress.ip_network("8.8.8.8/32")]), \
             mock.patch.object(public_authority, "NAMESERVERS", ["ns1.example.com."]), \
             mock.patch.object(public_authority, "SERVICES", {}), \
             mock.patch.object(public_authority, "EXTERNAL_RECORDS", {}), \
             tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(router, "STATE_DIR", Path(directory)):
            wires = public_authority.axfr_messages(query.to_wire(), "8.8.8.8")
            messages = [dns.message.from_wire(wire, xfr=True, origin=origin) for wire in wires]
            with mock.patch.object(router.dns.query, "xfr", return_value=iter(messages)):
                router.refresh_zone(origin)
            self.assertTrue(router.zone_path(origin).exists())
            self.assertIsNotNone(router.COPIES[origin].get_rdataset("@", dns.rdatatype.SOA))


if __name__ == "__main__":
    unittest.main()
