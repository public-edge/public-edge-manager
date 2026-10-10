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


def node_with_capacity(value):
    item = node()
    item["metadata"]["annotations"] = {"networking.re8ch.com/capacity-mbps": str(value)}
    return item


def assessment(name="edge", state="Ready", reachable=True):
    return {"metadata": {"name": "node-" + name},
            "spec": {"subjectRef": {"kind": "Node", "name": name}},
            "status": {"state": state, "validUntil": "2999-01-01T00:00:00Z",
                       "conditions": [{"type": "EvidenceReady", "status": "True"}],
                       "pathEvidence": {"currentPathMeasured": True, "reachable": reachable,
                                        "missingEvidence": []}}}


class ControllerTests(unittest.TestCase):
    def test_cloudflare_keeps_declared_secondary_ns_for_one_child_zone(self):
        records = {
            "service.re8ch.com": [{"id": "old", "type": "NS", "content": "stale.re8ch.com"}],
            "api.re8ch.com": [],
        }
        changes = []

        def cloudflare(method, path, payload=None):
            if path.startswith("/zones?"):
                return [{"id": "zone"}]
            if method == "GET":
                name = path.split("name=")[-1].split("&")[0]
                return records.get(name, [])
            changes.append((method, path, payload))
            return {}

        selected = [{"ns": "primary.re8ch.com.", "ip": "203.0.113.1"}]
        with mock.patch.object(controller, "CF_TOKEN", "test-token"), \
             mock.patch.object(controller, "PARENT_ZONE", "re8ch.com"), \
             mock.patch.object(controller, "CHILD_ZONES", ["service.re8ch.com", "api.re8ch.com"]), \
             mock.patch.object(controller, "SECONDARY_NAMESERVERS_BY_ZONE", {
                 "service.re8ch.com": {"ns1.he.net", "ns2.he.net"}}), \
             mock.patch.object(controller, "cloudflare", side_effect=cloudflare):
            controller.reconcile_cloudflare(selected)
        service_ns = {payload["content"] for method, _, payload in changes
                      if method == "POST" and payload["type"] == "NS"
                      and payload["name"] == "service.re8ch.com"}
        api_ns = {payload["content"] for method, _, payload in changes
                  if method == "POST" and payload["type"] == "NS"
                  and payload["name"] == "api.re8ch.com"}
        self.assertEqual(service_ns, {"primary.re8ch.com", "ns1.he.net", "ns2.he.net"})
        self.assertEqual(api_ns, {"primary.re8ch.com"})

    def test_lease_timestamp_uses_kubernetes_microtime(self):
        self.assertRegex(controller.now_rfc3339(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")

    def test_candidate_requires_global_external_ip_and_fresh_path(self):
        self.assertEqual(controller.global_external_ip(node()), "8.8.8.8")
        self.assertEqual(controller.global_external_ip(node(address="10.0.0.1")), "")
        self.assertTrue(controller.dns_host_eligible(node(), "8.8.8.8", "uid-1", True))
        self.assertFalse(controller.dns_host_eligible(node(), "", "uid-1", True))
        self.assertFalse(controller.dns_host_eligible(node(), "8.8.8.8", "uid-1", False))
        not_ready = node()
        not_ready["status"]["conditions"][0]["status"] = "False"
        self.assertFalse(controller.dns_host_eligible(not_ready, "8.8.8.8", "uid-1", True))
        with mock.patch.object(controller, "EXCLUDED_NODE_NAMES", {"edge"}):
            self.assertFalse(controller.dns_host_eligible(node(), "8.8.8.8", "uid-1", True))
        self.assertTrue(controller.assessment_ready(assessment()))
        self.assertFalse(controller.assessment_ready(assessment(state="Stale")))
        stale = assessment()
        stale["status"]["validUntil"] = "2000-01-01T00:00:00Z"
        self.assertFalse(controller.assessment_ready(stale, time.time()))

    def test_dns_authority_survives_stale_ingress_path_but_requires_public_dns(self):
        candidate = node(name="overseas-la")
        with mock.patch.object(controller, "PARENT_ZONE", "example.com"), \
             mock.patch.object(controller, "CHILD_ZONES", ["api.example.com", "service.example.com"]), \
             mock.patch.object(controller, "dns_transport_ready", return_value=True) as query:
            selected = controller.dns_authority_candidate(candidate, "8.8.8.8", "uid-1", 1000)
            self.assertEqual(selected["ip"], "8.8.8.8")
            self.assertEqual(query.call_count, 4)
            query.reset_mock()
            query.return_value = False
            self.assertIsNone(controller.dns_authority_candidate(candidate, "8.8.8.8", "uid-1", 1000))
            candidate["status"]["conditions"][0]["status"] = "False"
            query.reset_mock()
            self.assertIsNone(controller.dns_authority_candidate(candidate, "8.8.8.8", "uid-1", 1000))
            query.assert_not_called()

    def test_dns_transport_requires_majority_of_three_replies(self):
        with mock.patch.object(controller, "dns_query", side_effect=[False, True, True]) as query:
            self.assertTrue(controller.dns_transport_ready("8.8.8.8", "api.example.com", False))
            self.assertEqual(query.call_count, 3)
        with mock.patch.object(controller, "dns_query", side_effect=[True, False, False]) as query:
            self.assertFalse(controller.dns_transport_ready("8.8.8.8", "api.example.com", True))
            self.assertEqual(query.call_count, 3)

    def test_dns_authority_requires_service_address_answers(self):
        with mock.patch.object(controller, "PARENT_ZONE", "example.com"), \
             mock.patch.object(controller, "CHILD_ZONES", ["api.example.com"]), \
             mock.patch.object(controller, "ADDRESS_ZONES", ["api.example.com"]), \
             mock.patch.object(controller, "dns_transport_ready", return_value=True) as query:
            self.assertIsNotNone(controller.dns_authority_candidate(node(), "8.8.8.8", "uid-1", 1000))
            self.assertEqual({call.args[3] for call in query.call_args_list}, {1, 6})
            self.assertEqual(query.call_count, 4)

    def test_dns_dependency_admission_requires_both_planes_and_fresh_success(self):
        now = time.time()
        item = assessment()
        with mock.patch.object(controller, "REQUIRE_DNS_DEPENDENCIES", True):
            self.assertFalse(controller.assessment_ready(item, now))
            paths = [{"sourcePlane": plane, "observedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
                     for plane in ("host", "pod")]
            item["status"]["dependencyEvidence"] = {"dnsReady": True, "dnsComplete": True, "dnsPaths": paths}
            self.assertTrue(controller.assessment_ready(item, now))
            item["status"]["dependencyEvidence"]["dnsReady"] = False
            self.assertFalse(controller.assessment_ready(item, now))
            item["status"]["dependencyEvidence"]["dnsReady"] = True
            self.assertFalse(controller.assessment_ready(item, now + 121))
            paths.pop()
            self.assertFalse(controller.assessment_ready(item, now))

    def test_capacity_annotation_overrides_default(self):
        self.assertEqual(controller.capacity(node_with_capacity(3)), 3)
        self.assertLess(controller.capacity(node_with_capacity(3)), controller.MINIMUM_CAPACITY)

    def test_capacity_uses_public_edge_inventory(self):
        self.assertEqual(controller.capacity(node(name="overseas-la"), {
            "overseas-la": {"capacityMbps": 1000, "region": "us-ca"}}), 1000)

    def test_public_edge_inventory_overrides_stale_annotation(self):
        item = node_with_capacity(200)
        item["metadata"]["name"] = "r640"
        self.assertEqual(controller.capacity(item, {"r640": {
            "capacityMbps": 1000, "region": "cn-hunan"}}), 1000)

    def test_locality_uses_public_edge_inventory_over_node_label(self):
        self.assertEqual(
            controller.locality(node(name="r640", region="stale"), {
                "r640": {"capacityMbps": 200, "region": "cn-hunan"},
            }),
            ("CN", "cn-hunan"),
        )

    def test_shared_inventory_supplies_facts_without_restricting_membership(self):
        payload = {"data": {"nodes.json": '{"overseas-la":{"capacityMbps":1000,"region":"us-ca"}}'}}
        with mock.patch.object(controller, "CLUSTER_INVENTORY_NAMESPACE", "flux-system"), \
             mock.patch.object(controller, "CLUSTER_INVENTORY_CONFIGMAP", "cluster-node-inventory"), \
             mock.patch.object(controller, "api", return_value=payload) as api:
            inventory = controller.cluster_inventory_by_node()
            self.assertEqual(controller.capacity(node(name="overseas-la"), inventory), 1000)
            unlisted = node_with_capacity(400)
            unlisted["metadata"]["name"] = "new-edge"
            self.assertEqual(controller.capacity(unlisted, inventory), 400)
            self.assertGreaterEqual(controller.capacity(unlisted, inventory), controller.MINIMUM_CAPACITY)
            self.assertEqual(controller.locality(unlisted, inventory), ("US", "us-west"))
            api.assert_called_once_with("/api/v1/namespaces/flux-system/configmaps/cluster-node-inventory")

    def test_shared_inventory_rejects_invalid_capacity(self):
        payload = {"data": {"nodes.json": '{"overseas-la":{"capacityMbps":true,"region":"us-ca"}}'}}
        with mock.patch.object(controller, "CLUSTER_INVENTORY_NAMESPACE", "flux-system"), \
             mock.patch.object(controller, "CLUSTER_INVENTORY_CONFIGMAP", "cluster-node-inventory"), \
             mock.patch.object(controller, "api", return_value=payload):
            with self.assertRaises(ValueError):
                controller.cluster_inventory_by_node()

    def test_reconcile_status_uses_persisted_generation(self):
        desired = {
            "apiVersion": "networking.re8ch.com/v1alpha1", "kind": "PublicEdge",
            "metadata": {"name": "edge-a"}, "spec": {"capacityMbps": 1000},
            "status": {"observedAt": "now"},
        }
        with mock.patch.object(controller, "patch") as patch:
            patch.side_effect = [{"metadata": {"generation": 7}}, {}]
            controller.reconcile_object(desired, {"edge-a": {}})
        self.assertEqual(patch.call_args_list[1].args[1]["status"]["observedGeneration"], 7)

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
