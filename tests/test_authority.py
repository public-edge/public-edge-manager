
import os
import json
import tempfile

import struct
import time
import unittest
from unittest import mock
from public_edge_manager import authority


os.environ.setdefault("CANDIDATES_JSON", "[]")


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        authority.RECORDS_FILE = ""
        authority.RECORDS_DIGEST = ""
        authority.RUNTIME_FILE = ""
        authority.RUNTIME_MODEL = {}
        authority.NAMESERVERS_FILE = ""
        authority.NAMESERVERS_DIGEST = ""
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
        authority.AUTHORITY_ZONES = []
        authority.EXTERNAL_RECORDS = {}

    def test_nameserver_election_reloads_one_or_two_and_keeps_last_good_result(self):
        authority.NAMESERVERS = ["old.example.com."]
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as stream:
            authority.NAMESERVERS_FILE = stream.name
            json.dump({"nameservers": ["ns1.example.com.", "ns2.example.com."]}, stream)
            stream.flush()
            self.assertTrue(authority.reload_nameservers())
            self.assertEqual(authority.NAMESERVERS, ["ns1.example.com.", "ns2.example.com."])
            stream.seek(0)
            stream.truncate()
            json.dump({"nameservers": ["ns1.example.com."]}, stream)
            stream.flush()
            self.assertTrue(authority.reload_nameservers())
            self.assertEqual(authority.NAMESERVERS, ["ns1.example.com."])
            stream.seek(0)
            stream.truncate()
            json.dump({"nameservers": []}, stream)
            stream.flush()
            self.assertFalse(authority.reload_nameservers())
            self.assertEqual(authority.NAMESERVERS, ["ns1.example.com."])

    def test_record_config_reload_preserves_unchanged_service_health(self):
        authority.HEALTH["app"] = {"edge-1": {"ready": True}}
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as stream:
            authority.RECORDS_FILE = stream.name
            json.dump({"services": authority.SERVICE_DEFINITIONS, "zones": ["example.com."],
                       "externalRecords": {}}, stream)
            stream.flush()
            self.assertTrue(authority.reload_records())
            self.assertTrue(authority.HEALTH["app"]["edge-1"]["ready"])
            self.assertFalse(authority.reload_records())
            stream.seek(0)
            stream.truncate()
            json.dump({"services": authority.SERVICE_DEFINITIONS, "zones": ["example.com."],
                       "externalRecords": {"cdn.example.com.": [{"type": "CNAME", "value": "cdn.test.",
                                                                      "externalCDN": True}]}}, stream)
            stream.flush()
            self.assertTrue(authority.reload_records())
            self.assertIn("cdn.example.com.", authority.EXTERNAL_RECORDS)
            self.assertTrue(authority.HEALTH["app"]["edge-1"]["ready"])

    def test_invalid_record_reload_keeps_last_good_configuration(self):
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as stream:
            authority.RECORDS_FILE = stream.name
            stream.write('{"services":{},"zones":[],"externalRecords":{}}')
            stream.flush()
            self.assertFalse(authority.reload_records())
            self.assertIn("app.example.com.", authority.SERVICES)

    def test_runtime_overlay_registers_service_routes_and_model_without_git_values(self):
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as records, \
             tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as runtime:
            authority.RECORDS_FILE = records.name
            authority.RUNTIME_FILE = runtime.name
            json.dump({"services": authority.SERVICE_DEFINITIONS, "zones": ["example.com."],
                       "externalRecords": {}}, records)
            json.dump({
                "services": {"runtime.example.com.": {
                    "service": "runtime", "class": "api", "probePath": "/ready"
                }},
                "routes": {"runtime": [{"via": "canonical-gateway"}]},
                "model": {"capacityWeight": 17},
            }, runtime)
            records.flush(); runtime.flush()
            self.assertTrue(authority.reload_records())
            self.assertEqual(authority.SERVICES["runtime.example.com."], "runtime")
            self.assertEqual(authority.SERVICE_DEFINITIONS["runtime.example.com."]["paths"],
                             [{"via": "canonical-gateway"}])
            self.assertEqual(authority.RUNTIME_MODEL["capacityWeight"], 17)

    @staticmethod
    def dns_query(name, qtype):
        return (
            b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            + authority.encode_name(name)
            + struct.pack("!HH", qtype, 1)
        )

    @staticmethod
    def dns_records(response):
        _, _, qdcount, answer_count, authority_count, additional_count = struct.unpack(
            "!HHHHHH", response[:12]
        )
        offset = 12
        for _ in range(qdcount):
            _, offset = authority.parse_name(response, offset)
            offset += 4
        records = []
        for section, count in (
            ("answer", answer_count),
            ("authority", authority_count),
            ("additional", additional_count),
        ):
            for _ in range(count):
                name, offset = authority.parse_name(response, offset)
                qtype, qclass, ttl, length = struct.unpack("!HHIH", response[offset:offset + 10])
                offset += 10
                records.append((section, name, qtype, qclass, ttl, response[offset:offset + length]))
                offset += length
        return records

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

    def test_measured_public_path_can_qualify_without_node_ready(self):
        assessment = self.assessment(state="Partial", condition_status="False")
        assessment["status"]["nodeReady"] = False
        assessment["status"]["pathEvidence"]["missingEvidence"] = ["node-ready"]
        authority.FABRIC_ASSESSMENTS["edge-node"] = assessment
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Required"), \
             mock.patch.object(authority, "FABRIC_REQUIRE_NODE_READY", False):
            self.assertTrue(authority.fabric_evidence({"nodeName": "edge-node"})["eligible"])

    def test_missing_path_evidence_still_rejects_not_ready_node(self):
        assessment = self.assessment(state="Partial", condition_status="False")
        assessment["status"]["nodeReady"] = False
        assessment["status"]["pathEvidence"]["missingEvidence"] = ["node-ready", "reachable-current-path"]
        authority.FABRIC_ASSESSMENTS["edge-node"] = assessment
        with mock.patch.object(authority, "FABRIC_EVIDENCE_MODE", "Required"), \
             mock.patch.object(authority, "FABRIC_REQUIRE_NODE_READY", False):
            self.assertFalse(authority.fabric_evidence({"nodeName": "edge-node"})["eligible"])

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
        query = self.dns_query("app.example.com.", 1)
        response = authority.dns_response(query)
        self.assertEqual(struct.unpack("!H", response[6:8])[0], 0)

    def test_zone_apex_answers_ns_and_soa(self):
        authority.AUTHORITY_ZONES = ["edge.example."]
        with mock.patch.object(authority, "NAMESERVERS", ["ns1.edge.example.", "ns2.edge.example."]), \
             mock.patch.object(authority, "SOA_RNAME", "hostmaster.edge.example."):
            ns_response = authority.dns_response(self.dns_query("edge.example.", 2))
            soa_response = authority.dns_response(self.dns_query("edge.example.", 6))
        self.assertEqual(struct.unpack("!H", ns_response[6:8])[0], 2)
        self.assertEqual(
            [(section, name, qtype) for section, name, qtype, *_ in self.dns_records(ns_response)],
            [("answer", "edge.example.", 2), ("answer", "edge.example.", 2)],
        )
        self.assertEqual(struct.unpack("!H", soa_response[6:8])[0], 1)
        self.assertEqual(self.dns_records(soa_response)[0][1:3], ("edge.example.", 6))

    def test_unknown_name_in_zone_returns_nxdomain_with_soa(self):
        authority.AUTHORITY_ZONES = ["edge.example."]
        with mock.patch.object(authority, "NAMESERVERS", ["ns1.edge.example."]):
            response = authority.dns_response(self.dns_query("missing.edge.example.", 1))
        _, flags, _, answer_count, authority_count, _ = struct.unpack("!HHHHHH", response[:12])
        self.assertEqual(flags & 0x000F, 3)
        self.assertEqual(answer_count, 0)
        self.assertEqual(authority_count, 1)
        self.assertEqual(self.dns_records(response)[0][0:3], ("authority", "edge.example.", 6))

    def test_known_name_without_requested_type_returns_nodata_with_soa(self):
        authority.AUTHORITY_ZONES = ["example.com."]
        with mock.patch.object(authority, "NAMESERVERS", ["ns1.example.com."]):
            response = authority.dns_response(self.dns_query("app.example.com.", 28))
        _, flags, _, answer_count, authority_count, _ = struct.unpack("!HHHHHH", response[:12])
        self.assertEqual(flags & 0x000F, 0)
        self.assertEqual(answer_count, 0)
        self.assertEqual(authority_count, 1)

    def test_external_cdn_cname_is_answered_without_public_edge(self):
        authority.AUTHORITY_ZONES = ["edge.example."]
        authority.EXTERNAL_RECORDS = {"assets.edge.example.": [{
            "type": "CNAME", "value": "customer.cdn.example.",
            "externalCDN": True, "provider": "example-cdn",
        }]}
        response = authority.dns_response(self.dns_query("assets.edge.example.", 1))
        self.assertEqual(struct.unpack("!H", response[6:8])[0], 1)
        self.assertEqual(self.dns_records(response)[0][1:3], ("assets.edge.example.", 5))

    def test_external_record_other_type_is_nodata(self):
        authority.AUTHORITY_ZONES = ["edge.example."]
        authority.EXTERNAL_RECORDS = {"assets.edge.example.": [{
            "type": "A", "value": "192.0.2.10", "externalCDN": True,
        }]}
        with mock.patch.object(authority, "NAMESERVERS", ["ns1.edge.example."]):
            response = authority.dns_response(self.dns_query("assets.edge.example.", 28))
        _, flags, _, answer_count, authority_count, _ = struct.unpack("!HHHHHH", response[:12])
        self.assertEqual(flags & 0x000F, 0)
        self.assertEqual((answer_count, authority_count), (0, 1))

    def test_name_outside_configured_zones_is_refused(self):
        authority.AUTHORITY_ZONES = ["edge.example."]
        response = authority.dns_response(self.dns_query("app.example.com.", 1))
        _, flags, _, answer_count, authority_count, _ = struct.unpack("!HHHHHH", response[:12])
        self.assertEqual(flags & 0x000F, 5)
        self.assertEqual(flags & 0x0400, 0)
        self.assertEqual((answer_count, authority_count), (0, 0))

    def test_empty_zone_list_preserves_legacy_unknown_name_response(self):
        response = authority.dns_response(self.dns_query("missing.example.com.", 1))
        _, flags, _, _, authority_count, _ = struct.unpack("!HHHHHH", response[:12])
        self.assertEqual(flags & 0x000F, 3)
        self.assertNotEqual(flags & 0x0400, 0)
        self.assertEqual(authority_count, 0)

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
             mock.patch.object(authority, "KUBERNETES_API", "kubernetes"), \
             mock.patch.object(authority, "kubernetes_patch") as patch:
            authority.publish_edge_statuses()
        path, payload = patch.call_args.args
        self.assertEqual(path, "/apis/networking.re8ch.com/v1alpha1/publicedges/edge-a/status")
        self.assertEqual(payload["status"]["backendConditions"][0]["status"], "False")
        self.assertEqual(payload["status"]["services"]["app"]["statusCode"], 404)
        self.assertIsNone(payload["status"]["networkEvidence"]["score"])

    def test_legacy_publication_is_disabled_by_default(self):
        with mock.patch.object(authority, "PUBLICATION_ENABLED", False), \
             mock.patch.object(authority, "NODE", "publisher"), \
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
             mock.patch.object(authority, "PUBLICATION_ADAPTERS", adapters), \
             mock.patch.object(authority, "ranked", return_value=[selected]), \
             mock.patch.object(authority, "kubernetes_get", return_value={"metadata": {"annotations": {}}}), \
             mock.patch.object(authority, "kubernetes_patch") as patch:
            authority.publish_default_area()
        patch.assert_not_called()

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
