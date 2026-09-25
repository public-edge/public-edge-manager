import struct
import time
import unittest
from unittest import mock

from public_edge_manager import authority, controller


def node(name="edge", uid="uid-1", address="8.8.8.8", region="us-west"):
    return {
        "metadata": {"name": name, "uid": uid, "labels": {"topology.kubernetes.io/region": region}},
        "status": {"addresses": [{"type": "ExternalIP", "address": address}],
                   "conditions": [{"type": "Ready", "status": "True"}]},
    }


def assessment(name="edge", state="Ready", reachable=True):
    return {"metadata": {"name": "node-" + name},
            "spec": {"subjectRef": {"kind": "Node", "name": name}},
            "status": {"state": state, "validUntil": "2999-01-01T00:00:00Z",
                       "conditions": [{"type": "EvidenceReady", "status": "True"}],
                       "pathEvidence": {"currentPathMeasured": True, "reachable": reachable,
                                        "missingEvidence": []}}}


class ControllerTests(unittest.TestCase):
    def test_candidate_requires_global_external_ip_and_fresh_path(self):
        self.assertEqual(controller.global_external_ip(node()), "8.8.8.8")
        self.assertEqual(controller.global_external_ip(node(address="10.0.0.1")), "")
        self.assertTrue(controller.assessment_ready(assessment()))
        self.assertFalse(controller.assessment_ready(assessment(state="Stale")))
        stale = assessment()
        stale["status"]["validUntil"] = "2000-01-01T00:00:00Z"
        self.assertFalse(controller.assessment_ready(stale, time.time()))

    def test_names_are_stable_hashes_and_do_not_embed_node_name(self):
        with mock.patch.object(controller, "PARENT_ZONE", "example.com"):
            self.assertEqual(controller.object_name("uid-1"), controller.object_name("uid-1"))
            self.assertNotIn("edge", controller.nameserver_name("uid-1"))
            self.assertTrue(controller.nameserver_name("uid-1").endswith(".example.com."))

    def test_gateway_is_selected_by_generic_label_and_programmed_state(self):
        gateway = {"metadata": {"name": "gateway", "namespace": "system", "labels": {
                       "networking.re8ch.com/public-edge-gateway": "true"}, "creationTimestamp": "2026-01-01"},
                   "status": {"conditions": [{"type": "Programmed", "status": "True"}],
                              "addresses": [{"value": "10.0.0.80"}]}}
        selected, address = controller.select_gateway([gateway])
        self.assertIs(selected, gateway)
        self.assertEqual(address, "10.0.0.80")

    def test_edge_transport_is_independent_of_backend_health(self):
        authority.CANDIDATES[:] = [{"id": "edge-a", "region": "us", "area": "US",
                                    "ip": "192.0.2.1", "capacityMbps": 100,
                                    "edgeReady": True, "probes": {"app": "https://app.example/"}}]
        authority.HEALTH = {"app": {"edge-a": {"ready": False, "observedAt": int(time.time())}}}
        with mock.patch.object(authority, "readiness_gate_ready", return_value=True), \
             mock.patch.object(authority, "fabric_evidence", return_value={"eligible": True}):
            ranked = authority.ranked("app", "US")
        self.assertEqual(ranked[0]["state"], "ready")
        self.assertFalse(ranked[0]["serviceBackendReady"])

    def test_ecs_drives_area_instead_of_resolver_address(self):
        qname = authority.encode_name("app.example.com.")
        question = qname + struct.pack("!HH", 1, 1)
        ecs = struct.pack("!HHHBB", 8, 7, 1, 24, 0) + bytes([203, 0, 113])
        opt = b"\0" + struct.pack("!HHIH", 41, 1232, 0, len(ecs)) + ecs
        packet = b"\x12\x34" + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 1) + question + opt
        self.assertEqual(authority.ecs_address(packet, 12 + len(question)), "203.0.113.0")
        with mock.patch.object(authority, "CLIENT_AREA_CIDRS", {"US": ["203.0.113.0/24"]}):
            self.assertEqual(authority.client_area("203.0.113.0"), "US")
