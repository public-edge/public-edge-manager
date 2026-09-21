
import os

import struct
import time
import unittest
from unittest import mock
from public_edge_manager import authority


os.environ.setdefault("CANDIDATES_JSON", "[]")


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        authority.SERVICE_DEFINITIONS = {
            "app.example.com.": {"service": "app", "class": "web", "probePath": "/healthz"}
        }
        authority.SERVICES = {"app.example.com.": "app"}
        authority.HEALTH = {"app": {}}
        authority.CANDIDATES[:] = []
        authority.FABRIC_ASSESSMENTS.clear()
        authority.FABRIC_API_AVAILABLE = False
        authority.FABRIC_NODE_READINESS.clear()
        authority.FABRIC_NODE_API_AVAILABLE = False
        authority.FABRIC_EVIDENCE_MODE = "Disabled"
        authority.FABRIC_REQUIRE_NODE_READY = False
        authority.FABRIC_EVIDENCE_ALLOWED_STATES = {"Ready", "Partial"}

    @staticmethod
    def assessment(node="edge-node", state="Ready", valid_until="2099-01-01T00:00:00Z",
                   condition_status="True"):
        return {
            "metadata": {"name": f"node-{node}"},
            "spec": {
                "subjectRef": {"apiVersion": "v1", "kind": "Node", "name": node},
                "scope": {"plane": "host-and-pod", "direction": "bidirectional", "protocol": "mixed"},
            },
            "status": {
                "state": state,
                "observedAt": "2026-09-08T00:00:00Z",
                "validUntil": valid_until,
                "nodeReady": True,
                "pathEvidence": {"currentPathMeasured": True, "reachable": True,
                                 "viableAlternatives": 1, "freshPlanes": ["host", "pod"]},
                "conditions": [{"type": "EvidenceReady", "status": condition_status,
                                "reason": "AllDimensionsAvailable"}],
            },
        }

    def test_refresh_fabric_assessments_indexes_node_subjects(self):
        payload = {"items": [self.assessment(), {
            "metadata": {"name": "unsupported"},
            "spec": {"subjectRef": {"kind": "Service", "name": "app"},
                     "scope": {"plane": "pod"}},
        }]}
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Optional"), \
             mock.patch.object(authority, "kubernetes_get", return_value=payload):
            authority.refresh_fabric_assessments()
        self.assertTrue(authority.FABRIC_API_AVAILABLE)
        self.assertEqual(set(authority.FABRIC_ASSESSMENTS), {"edge-node"})

    def test_refresh_fabric_assessments_caches_exact_node_ready_condition(self):
        responses = [
            {"items": [self.assessment()]},
            {"items": [{"metadata": {"name": "edge-node"}, "status": {"conditions": [
                {"type": "Ready", "status": "False"},
            ]}}]},
        ]
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Required"), \
             mock.patch.object(authority, "FABRIC_REQUIRE_NODE_READY", True), \
             mock.patch.object(authority, "kubernetes_get", side_effect=responses):
            authority.refresh_fabric_assessments()
        self.assertTrue(authority.FABRIC_NODE_API_AVAILABLE)
        self.assertFalse(authority.FABRIC_NODE_READINESS["edge-node"])

    def test_required_evidence_rejects_not_ready_node_even_with_fresh_npa(self):
        authority.FABRIC_ASSESSMENTS["edge-node"] = self.assessment()
        authority.FABRIC_NODE_READINESS["edge-node"] = False
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Required"), \
             mock.patch.object(authority, "FABRIC_REQUIRE_NODE_READY", True), \
             mock.patch.object(authority, "FABRIC_API_AVAILABLE", True), \
             mock.patch.object(authority, "FABRIC_NODE_API_AVAILABLE", True):
            result = authority.fabric_evidence({"nodeName": "edge-node"})
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "NodeNotReady")

    def test_required_evidence_fails_closed_when_node_readiness_is_unavailable(self):
        authority.FABRIC_ASSESSMENTS["edge-node"] = self.assessment()
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Required"), \
             mock.patch.object(authority, "FABRIC_REQUIRE_NODE_READY", True), \
             mock.patch.object(authority, "FABRIC_NODE_API_AVAILABLE", False):
            result = authority.fabric_evidence({"nodeName": "edge-node"})
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "NodeReadinessUnavailable")

    def test_optional_fabric_evidence_allows_absent_provider(self):
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Optional"):
            result = authority.fabric_evidence({"nodeName": "edge-node"})
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "ProviderUnavailable")

    def test_required_fabric_evidence_rejects_absent_assessment(self):
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Required"), \
             mock.patch.object(authority, "FABRIC_API_AVAILABLE", True):
            result = authority.fabric_evidence({"nodeName": "edge-node"})
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "AssessmentNotFound")

    def test_shadow_fabric_evidence_reports_without_gating_or_scoring(self):
        authority.FABRIC_ASSESSMENTS["edge-node"] = self.assessment(
            state="Stale", valid_until="2026-09-08T00:00:30Z", condition_status="False"
        )
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Shadow"):
            result = authority.fabric_evidence({"nodeName": "edge-node"}, now=1788825700)
        self.assertTrue(result["eligible"])
        self.assertTrue(result["wouldReject"])
        self.assertNotIn("score", result)

    def test_matching_stale_assessment_fails_closed(self):
        authority.FABRIC_ASSESSMENTS["edge-node"] = self.assessment(
            valid_until="2026-09-08T00:00:30Z"
        )
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Optional"):
            result = authority.fabric_evidence({"nodeName": "edge-node"}, now=1788825700)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "EvidenceExpired")

    def test_fresh_assessment_is_only_an_eligibility_gate(self):
        authority.FABRIC_ASSESSMENTS["edge-node"] = self.assessment()
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Required"):
            result = authority.fabric_evidence({"nodeName": "edge-node"}, now=1788825600)
        self.assertTrue(result["eligible"])
        self.assertNotIn("score", result)
        self.assertTrue(result["pathEvidence"]["reachable"])

    def test_required_rejects_unexecuted_current_path(self):
        assessment = self.assessment()
        assessment["status"]["pathEvidence"]["currentPathMeasured"] = False
        authority.FABRIC_ASSESSMENTS["edge-node"] = assessment
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Required"):
            result = authority.fabric_evidence({"nodeName": "edge-node"}, now=1788825600)
        self.assertFalse(result["eligible"])

    def test_disabled_and_draining_edges_are_not_candidates(self):
        payload = {"items": [
            {"metadata": {"name": "disabled"}, "spec": {"enabled": False, "draining": False}},
            {"metadata": {"name": "draining"}, "spec": {"enabled": True, "draining": True}},
        ]}
        original = authority.kubernetes_get
        authority.kubernetes_get = lambda _path: payload
        try:
            self.assertEqual(authority.candidates_from_public_edges(), [])
        finally:
            authority.kubernetes_get = original

    def test_empty_inventory_removes_stale_candidate_and_health(self):
        authority.CANDIDATES[:] = [{"id": "us-edge-50"}]
        authority.HEALTH["app"]["us-edge-50"] = {"ready": True}
        with mock.patch.object(authority, "KUBERNETES_API", "kubernetes"), \
             mock.patch.object(authority, "kubernetes_get", return_value={"items": []}):
            authority.refresh_candidates()
        self.assertEqual(authority.CANDIDATES, [])
        self.assertEqual(authority.HEALTH["app"], {})

    def test_inventory_failure_fails_closed(self):
        authority.CANDIDATES[:] = [{"id": "us-edge-50"}]
        with mock.patch.object(authority, "KUBERNETES_API", "kubernetes"), \
             mock.patch.object(authority, "kubernetes_get", return_value=None):
            authority.refresh_candidates()
        self.assertEqual(authority.CANDIDATES, [])

    def test_stale_service_probe_cannot_be_ranked_ready(self):
        authority.CANDIDATES[:] = [{"id": "edge-a", "region": "test", "ip": "192.0.2.10",
                                   "probes": {"app": "https://app.example.com/"}}]
        authority.HEALTH["app"]["edge-a"] = {"ready": True, "observedAt": 1}
        self.assertEqual(authority.ranked("app")[0]["state"], "unavailable")
        self.assertEqual(authority.ranked("app")[0]["reason"], "ServiceProbeStale")

    def test_public_edge_builds_service_probe(self):
        payload = {"items": [{
            "metadata": {"name": "edge-a"},
            "spec": {
                "enabled": True, "draining": False, "region": "test", "gatewayVIP": "10.251.0.4",
                "endpoint": {"type": "PublicIP", "value": "192.0.2.10"},
                "serviceClasses": ["web"],
            },
        }]}
        original = authority.kubernetes_get
        authority.kubernetes_get = lambda _path: payload
        try:
            candidates = authority.candidates_from_public_edges()
        finally:
            authority.kubernetes_get = original
        self.assertEqual(candidates[0]["probes"]["app"], "https://app.example.com/healthz")

    def test_probe_all_bounds_parallel_workers(self):
        authority.CANDIDATES[:] = [{
            "id": "edge-a", "probes": {
                f"service-{index}": f"https://service-{index}.example.com/healthz"
                for index in range(20)
            },
        }]
        submitted = []

        class Future:
            def result(self):
                return None

        class Executor:
            def __init__(self, max_workers, **_kwargs):
                self.max_workers = max_workers

            def __enter__(self):
                submitted.append(("workers", self.max_workers))
                return self

            def __exit__(self, *_args):
                return False

            def submit(self, function, *args):
                submitted.append((function, args))
                return Future()

        with mock.patch.object(authority, "refresh_candidates"), \
             mock.patch.object(authority, "refresh_fabric_assessments"), \
             mock.patch.object(authority, "ThreadPoolExecutor", Executor), \
             mock.patch.object(authority, "PROBE_MAX_WORKERS", 6):
            authority.probe_all()

        self.assertEqual(submitted[0], ("workers", 6))
        self.assertEqual(len(submitted) - 1, 20)

    def test_dns_fails_closed_without_ready_edge(self):
        query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + authority.encode_name("app.example.com.") + struct.pack("!HH", 1, 1)
        response = authority.dns_response(query)
        self.assertEqual(struct.unpack("!H", response[6:8])[0], 0)

    def test_explicit_statuses_reject_redirect_loop(self):
        authority.SERVICE_DEFINITIONS = {
            "login.example.com.": {
                "service": "dex",
                "class": "web",
                "probePath": "/.well-known/openid-configuration",
                "acceptedStatuses": [200],
            }
        }
        self.assertTrue(authority.accepted_probe_status("dex", 200))
        self.assertFalse(authority.accepted_probe_status("dex", 308))

    def test_area_prefers_high_capacity_local_edge(self):
        original_area = authority.AREA
        authority.AREA = "CN"
        authority.CANDIDATES[:] = [
            {"id": "small-edge", "region": "region-a", "area": "CN", "ip": "192.0.2.1", "capacityMbps": 100, "priority": 0, "probes": {"app": "https://app.example.com/"}},
            {"id": "large-edge", "region": "region-b", "area": "CN", "ip": "192.0.2.2", "capacityMbps": 1000, "priority": 0, "probes": {"app": "https://app.example.com/"}},
        ]
        authority.HEALTH["app"] = {name: {"ready": True, "latencyMs": 10, "observedAt": int(time.time())} for name in ("small-edge", "large-edge")}
        try:
            self.assertEqual(authority.ranked("app")[0]["id"], "large-edge")
        finally:
            authority.AREA = original_area

    def test_us_prefers_regional_relay_over_remote_capacity(self):
        original_area = authority.AREA
        authority.AREA = "US"
        authority.CANDIDATES[:] = [
            {"id": "us-relay", "region": "los-angeles", "area": "US", "ip": "192.0.2.3", "capacityMbps": 100, "priority": 0, "forwarding": {"mode": "RegionalRelay", "originArea": "CN"}, "probes": {"app": "https://app.example.com/"}},
            {"id": "remote-origin", "region": "region-b", "area": "CN", "ip": "192.0.2.2", "capacityMbps": 1000, "priority": 0, "probes": {"app": "https://app.example.com/"}},
        ]
        authority.HEALTH["app"] = {name: {"ready": True, "latencyMs": 10, "observedAt": int(time.time())} for name in ("us-relay", "remote-origin")}
        try:
            self.assertEqual(authority.ranked("app")[0]["id"], "us-relay")
        finally:
            authority.AREA = original_area

    def test_publisher_refreshes_per_service_edge_status(self):
        authority.CANDIDATES[:] = [{
            "id": "edge-a", "region": "test", "area": "test", "ip": "192.0.2.10",
            "probes": {"app": "https://app.example.com/healthz"},
        }]
        authority.HEALTH = {"app": {"edge-a": {
            "ready": False, "statusCode": 404, "latencyMs": 9,
            "observedAt": 123, "failure": "unexpected status",
        }}}
        with mock.patch.object(authority, "NODE", "publisher"), \
             mock.patch.object(authority, "PUBLISHER_NODE", "publisher"), \
             mock.patch.object(authority, "KUBERNETES_API", "kubernetes"), \
             mock.patch.object(authority, "kubernetes_patch") as patch:
            authority.publish_edge_statuses()
        path, payload = patch.call_args.args
        self.assertEqual(path, "/apis/networking.re8ch.com/v1alpha1/publicedges/edge-a/status")
        self.assertEqual(payload["status"]["conditions"][0]["status"], "False")
        self.assertEqual(payload["status"]["services"]["app"]["statusCode"], 404)
        self.assertIsNone(payload["status"]["networkEvidence"]["score"])

    def test_legacy_publication_is_disabled_by_default(self):
        with mock.patch.object(authority, "PUBLICATION_ENABLED", False), \
             mock.patch.object(authority, "NODE", "publisher"), \
             mock.patch.object(authority, "PUBLISHER_NODE", "publisher"), \
             mock.patch.object(authority, "PUBLICATION_REFS", {"app": {"namespace": "default", "name": "app"}}), \
             mock.patch.object(authority, "kubernetes_get") as get:
            authority.publish_default_area()
        get.assert_not_called()

    def test_named_publication_adapters_patch_each_provider_object(self):
        adapters = {
            "alidns": {
                "provider": "alibabacloud",
                "refs": {"app": {"namespace": "dns", "name": "app-alidns"}},
            },
            "dnspod": {
                "provider": "tencent-dnspod",
                "refs": {"app": {"namespace": "dns", "name": "app-dnspod"}},
            },
        }
        selected = {"id": "edge-a", "ip": "192.0.2.10", "state": "ready"}
        with mock.patch.object(authority, "PUBLICATION_ENABLED", True), \
             mock.patch.object(authority, "NODE", "publisher"), \
             mock.patch.object(authority, "PUBLISHER_NODE", "publisher"), \
             mock.patch.object(authority, "PUBLICATION_ADAPTERS", adapters), \
             mock.patch.object(authority, "ranked", return_value=[selected]), \
             mock.patch.object(authority, "kubernetes_get", return_value={"metadata": {"annotations": {}}}), \
             mock.patch.object(authority, "kubernetes_patch") as patch:
            authority.publish_default_area()
        self.assertEqual(patch.call_count, 2)
        paths = {call.args[0] for call in patch.call_args_list}
        self.assertEqual(paths, {
            "/apis/networking.k8s.io/v1/namespaces/dns/ingresses/app-alidns",
            "/apis/networking.k8s.io/v1/namespaces/dns/ingresses/app-dnspod",
        })
        annotations = [call.args[1]["metadata"]["annotations"] for call in patch.call_args_list]
        self.assertEqual({item[f"{authority.API_GROUP}/dns-provider"] for item in annotations}, {
            "alibabacloud", "tencent-dnspod",
        })

    def test_disabled_publication_adapter_is_skipped(self):
        adapters = {
            "esa": {
                "enabled": False,
                "provider": "alibaba-esa",
                "refs": {"app": {"namespace": "dns", "name": "app-esa"}},
            }
        }
        with mock.patch.object(authority, "PUBLICATION_ENABLED", True), \
             mock.patch.object(authority, "NODE", "publisher"), \
             mock.patch.object(authority, "PUBLISHER_NODE", "publisher"), \
             mock.patch.object(authority, "PUBLICATION_ADAPTERS", adapters), \
             mock.patch.object(authority, "kubernetes_get") as get:
            authority.publish_default_area()
        get.assert_not_called()

    def test_publication_adapters_reject_shared_objects(self):
        adapters = {
            name: {
                "provider": name,
                "refs": {"app": {"namespace": "dns", "name": "shared"}},
            }
            for name in ("alidns", "dnspod")
        }
        with mock.patch.object(authority, "PUBLICATION_ADAPTERS", adapters):
            with self.assertRaisesRegex(RuntimeError, "is shared by adapters"):
                authority.validate_publication_adapters()

    def test_custom_api_group_is_used_for_status(self):
        authority.CANDIDATES[:] = [{
            "id": "edge-a", "region": "test", "area": "test", "ip": "192.0.2.10",
            "probes": {"app": "https://app.example.com/healthz"},
        }]
        authority.HEALTH = {"app": {"edge-a": {"ready": True}}}
        with mock.patch.object(authority, "API_GROUP", "networking.example.org"), \
             mock.patch.object(authority, "NODE", "publisher"), \
             mock.patch.object(authority, "PUBLISHER_NODE", "publisher"), \
             mock.patch.object(authority, "KUBERNETES_API", "kubernetes"), \
             mock.patch.object(authority, "kubernetes_patch") as patch:
            authority.publish_edge_statuses()
        self.assertEqual(
            patch.call_args.args[0],
            "/apis/networking.example.org/v1alpha1/publicedges/edge-a/status",
        )

    def test_readiness_gate_matches_selected_ready_address(self):
        gate = {
            "app": {
                "configMap": {"namespace": "system", "name": "authority", "dataKey": "authority.json"},
                "requiredFields": {"source": "election"},
                "addressPath": ["leader", "address"],
                "endpointSlice": {"namespace": "backend", "name": "primary"},
            }
        }
        responses = [
            {"data": {"authority.json": '{"source":"election","leader":{"address":"10.0.0.8"}}'}},
            {"endpoints": [{"conditions": {"ready": True}, "addresses": ["10.0.0.8"]}]},
        ]
        with mock.patch.object(authority, "READINESS_GATES", gate), \
             mock.patch.object(authority, "kubernetes_get", side_effect=responses):
            self.assertTrue(authority.readiness_gate_ready("app"))

    def test_readiness_gate_fails_closed_on_conflicting_evidence(self):
        gate = {
            "app": {
                "configMap": {"namespace": "system", "name": "authority", "dataKey": "authority.json"},
                "addressPath": ["leader", "address"],
                "endpointSlice": {"namespace": "backend", "name": "primary"},
            }
        }
        responses = [
            {"data": {"authority.json": '{"leader":{"address":"10.0.0.8"}}'}},
            {"endpoints": [{"conditions": {"ready": True}, "addresses": ["10.0.0.9"]}]},
        ]
        with mock.patch.object(authority, "READINESS_GATES", gate), \
             mock.patch.object(authority, "kubernetes_get", side_effect=responses):
            self.assertFalse(authority.readiness_gate_ready("app"))


if __name__ == "__main__":
    unittest.main()
