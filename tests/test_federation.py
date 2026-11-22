import copy
import time
import struct
import socket
import unittest
from contextlib import ExitStack
from unittest import mock

from public_edge_manager import authority
from public_edge_manager.federation import resolve


class FederationTests(unittest.TestCase):
    def setUp(self):
        self.discovery = {"source": "AmongCluster", "clusterRef": "provider", "serviceRef": "spiffe://provider.example/ns/apps/service/canary", "protocol": "https", "endpointId": "site-a",
                          "assessmentRef": {"name": "service-canary", "uid": "npa-uid", "sourceNode": "consumer-node", "targetId": "canary-path", "paddingBytes": [0, 2048]},
                          "advertisementRef": {"namespace": "peering", "name": "canary", "uid": "ad-uid"},
                          "importedServiceRef": {"namespace": "peering", "name": "canary-import", "uid": "import-uid"}}
        ingress = {"id":"site-a", "site":"site-a", "region":"test-region", "protocol": "https", "address": "192.0.2.10", "port": 8443, "hostname": "canary.example.com", "path": "/health", "revision": "reviewed-1"}
        self.ad = {"metadata": {"name": "canary", "uid": "ad-uid", "generation": 1, "resourceVersion": "10", "annotations": {"peering.re8ch.com/last-published-at": "1970-01-01T00:16:40Z"}},
                   "spec": {"publisherRef": "provider", "serviceIdentity": self.discovery["serviceRef"], "protocol": "https", "targetPeers": ["provider-consumer"], "ttlSeconds": 60, "publicIngress": {"endpoints":[ingress]}}}
        self.imported = {"metadata": {"name": "canary-import", "uid": "import-uid", "generation": 1, "resourceVersion": "20"},
                         "spec": {"advertisementRef": "canary", "peerRef": "provider-consumer"},
                         "status": {"state": "Ready", "observedGeneration": 1, "advertisementUID": "ad-uid", "advertisementGeneration": 1, "publicIngress": {"endpoints":[copy.deepcopy(ingress)]}}}
        self.endpoint = self.ad["spec"]["publicIngress"]["endpoints"][0]
        self.edge = {"metadata": {"name": "canary-edge", "generation": 1}, "spec": {"enabled": True, "nodeName": "path-subject", "serviceClasses": ["web"], "region": "test-region", "gatewayVIP": "192.0.2.1", "endpoint": {"type": "PublicIP", "value": "192.0.2.5"}, "discovery": self.discovery}}
        self.services = {"canary.example.com.": {"service": "canary", "class": "web"}}
        self.npa = {"metadata": {"name": "service-canary", "uid": "npa-uid", "generation": 2, "resourceVersion": "30"},
                    "spec": {"subjectRef": {"apiVersion": "v1", "kind": "Service", "namespace": "apps", "name": "canary"},
                             "scope": {"plane": "host-and-pod", "direction": "bidirectional", "protocol": "tcp", "sourceNode": "consumer-node", "targetId": "canary-path", "endpoint": "192.0.2.10", "port": 8443, "serverName": "canary.example.com", "path": "/health", "paddingBytes": [0, 2048]}},
                    "status": {"state": "Ready", "observedAt": "1970-01-01T00:16:40Z", "validUntil": "1970-01-01T00:30:00Z", "conditions": [{"type": "EvidenceReady", "status": "True"}], "pathEvidence": {"currentPathMeasured": True, "reachable": True},
                               "serviceEvidence": {"definition": "tls-transfer-v1", "required": True, "complete": True, "withinLatencyBudget": True, "maximumTotalMs": 1000}}}


    def get(self, path):
        if "/networkpathassessments/" in path:
            return self.npa
        if path.endswith("/publicedges"):
            return {"items": [self.edge]}
        return self.ad if "/serviceadvertisements/" in path else self.imported

    def test_typed_ingress_identity_and_expiry(self):
        observed = resolve(self.discovery, self.get, 1001)
        self.assertTrue(observed["eligible"])
        self.assertEqual(observed["publicIngress"]["port"], 8443)
        self.assertEqual(observed["advertisementRef"]["resourceVersion"], "10")
        self.assertEqual(resolve(self.discovery, self.get, 1060)["reason"], "FederationExpired")
        self.ad["spec"]["revoked"] = True
        self.assertEqual(resolve(self.discovery, self.get, 1001)["reason"], "FederationRevoked")

    def test_mismatched_owner_uid_protocol_and_import_fail_closed(self):
        for target, key, value in [(self.ad["metadata"], "uid", "recreated-ad"), (self.ad["spec"], "publisherRef", "other"), (self.ad["spec"], "protocol", "quic"), (self.imported["status"], "advertisementGeneration", 0)]:
            old = target[key]
            target[key] = value
            self.assertFalse(resolve(self.discovery, self.get, 1001)["eligible"])
            target[key] = old
        self.endpoint["protocol"] = "quic"
        self.assertFalse(resolve(self.discovery, self.get, 1001)["eligible"])

    def runtime(self):
        stack = ExitStack()
        for name, value in {"NODE": "consumer-node", "KUBERNETES_API": "configured", "SERVICE_DEFINITIONS": self.services,
                            "CANDIDATES": [], "HEALTH": {"canary": {}}, "FABRIC_EVIDENCE_MODE": "Disabled",
                            "FABRIC_REQUIRE_NODE_READY": False, "FABRIC_API_AVAILABLE": True,
                            "FABRIC_API_OUTAGE": False, "GLOBALPING_QUALITY_MODE": "Shadow",
                            "READINESS_GATES": {}, "SELECTIONS": {}, "SERVICES": {"canary.example.com.": "canary"},
                            "AUTHORITY_ZONES": ["example.com."], "EXTERNAL_RECORDS": {}, "FABRIC_ASSESSMENTS": {"path-subject": self.npa}}.items():
            stack.enter_context(mock.patch.object(authority, name, value))
        stack.enter_context(mock.patch.object(authority, "kubernetes_get", side_effect=self.get))
        stack.enter_context(mock.patch.object(authority.time, "time", return_value=1001))
        return stack

    def qualify(self):
        candidate = authority.CANDIDATES[0]
        authority.HEALTH["canary"]["canary-edge"] = {"ready": True, "observedAt": 1001, "endpointRevision": candidate["endpointRevision"]}
        return candidate

    def test_endpoint_change_clears_health_and_rejects_inflight_old_result(self):
        with self.runtime():
            authority.refresh_candidates()
            old = self.qualify()
            self.assertEqual(authority.ranked("canary")[0]["state"], "ready")
            self.assertEqual(authority.stable_selected("canary", authority.ranked("canary"))["ip"], "192.0.2.10")
            self.endpoint["address"] = "192.0.2.11"
            self.endpoint["revision"] = "reviewed-2"
            self.imported["status"]["publicIngress"] = copy.deepcopy(self.ad["spec"]["publicIngress"])
            authority.refresh_candidates()
            self.assertEqual(authority.CANDIDATES[0]["ip"], "192.0.2.11")
            self.assertNotIn("canary-edge", authority.HEALTH["canary"])
            self.assertIsNone(authority.stable_selected("canary", authority.ranked("canary")))
            authority.HEALTH["canary"]["canary-edge"] = {"ready": True, "observedAt": 1001, "endpointRevision": old["endpointRevision"]}
            self.assertEqual(authority.ranked("canary")[0]["state"], "unavailable")
            self.npa["spec"]["scope"]["endpoint"] = "192.0.2.11"
            authority.refresh_candidates()
            self.qualify()
            self.assertEqual(authority.ranked("canary")[0]["state"], "ready")
            self.assertEqual(authority.stable_selected("canary", authority.ranked("canary"))["ip"], "192.0.2.11")
            self.ad["spec"]["revoked"] = True
            authority.refresh_candidates()
            self.assertEqual(authority.ranked("canary")[0]["state"], "unavailable")
            self.assertIsNone(authority.stable_selected("canary", authority.ranked("canary")))

    def test_ref_requires_fresh_af_even_if_chart_mode_disabled(self):
        with self.runtime():
            authority.refresh_candidates()
            candidate = self.qualify()
            self.assertTrue(authority.fabric_evidence(candidate)["eligible"])
            candidate["serviceAssessment"] = None
            self.assertFalse(authority.fabric_evidence(candidate)["eligible"])

    def test_cached_advertisement_expires_without_api_refresh(self):
        with self.runtime():
            authority.refresh_candidates()
            candidate = self.qualify()
            self.assertFalse(authority.fabric_evidence(candidate, now=1060)["eligible"])

    def test_import_must_observe_changed_endpoint_before_it_is_used(self):
        self.endpoint["address"] = "192.0.2.11"
        self.assertEqual(resolve(self.discovery, self.get, 1001)["reason"], "FederationIngressNotObserved")

    def test_authoritative_dns_tracks_new_endpoint_and_withdrawal(self):
        query = struct.pack("!HHHHHH", 7, 0x0100, 1, 0, 0, 0) + authority.encode_name("canary.example.com.") + struct.pack("!HH", 1, 1)
        with self.runtime():
            authority.refresh_candidates()
            self.qualify()
            response = authority.dns_response(query)
            self.assertIn(socket.inet_aton("192.0.2.10"), response)
            self.endpoint["address"] = "192.0.2.11"
            self.imported["status"]["publicIngress"] = copy.deepcopy(self.ad["spec"]["publicIngress"])
            authority.refresh_candidates()
            response = authority.dns_response(query)
            self.assertNotIn(socket.inet_aton("192.0.2.10"), response)
            self.npa["spec"]["scope"]["endpoint"] = "192.0.2.11"
            authority.refresh_candidates()
            self.qualify()
            self.assertIn(socket.inet_aton("192.0.2.11"), authority.dns_response(query))
            self.ad["spec"]["revoked"] = True
            authority.refresh_candidates()
            response = authority.dns_response(query)
            self.assertNotIn(socket.inet_aton("192.0.2.11"), response)

    def test_service_scope_cannot_cross_source_endpoint_or_size(self):
        with self.runtime():
            authority.refresh_candidates()
            candidate = self.qualify()
            for key, bad in [("sourceNode", "other-node"), ("endpoint", "192.0.2.99"), ("paddingBytes", [0]), ("targetId", "other-target")]:
                old = self.npa["spec"]["scope"][key]
                self.npa["spec"]["scope"][key] = bad
                authority.refresh_candidates()
                self.assertFalse(authority.fabric_evidence(authority.CANDIDATES[0])["eligible"])
                self.npa["spec"]["scope"][key] = old

    def test_assessment_map_uses_only_this_authority_source(self):
        with self.runtime():
            ref = self.discovery.pop("assessmentRef")
            self.discovery["assessmentRefs"] = {"other-node": {**ref, "sourceNode": "other-node"}}
            authority.refresh_candidates()
            self.assertFalse(authority.fabric_evidence(authority.CANDIDATES[0])["eligible"])
            self.discovery["assessmentRefs"]["consumer-node"] = ref
            authority.refresh_candidates()
            self.assertTrue(authority.fabric_evidence(authority.CANDIDATES[0])["eligible"])

    def test_optional_configuration_digest_is_strict_when_bound(self):
        with self.runtime():
            self.discovery["assessmentRef"]["expectedConfigurationDigest"] = "sha256-reviewed"
            authority.refresh_candidates()
            self.assertFalse(authority.fabric_evidence(authority.CANDIDATES[0])["eligible"])
            self.npa["spec"]["scope"]["targetConfigurationDigest"] = "different-path-config"
            authority.refresh_candidates()
            self.assertFalse(authority.fabric_evidence(authority.CANDIDATES[0])["eligible"])
            self.npa["spec"]["scope"]["targetConfigurationDigest"] = "sha256-reviewed"
            authority.refresh_candidates()
            decision = authority.fabric_evidence(authority.CANDIDATES[0])
            self.assertTrue(decision["eligible"])
            self.assertEqual(decision["assessmentRef"]["scope"]["targetConfigurationDigest"], "sha256-reviewed")

    def test_inflight_old_probe_cannot_overwrite_new_endpoint_health(self):
        with self.runtime():
            authority.refresh_candidates()
            old = copy.deepcopy(self.qualify())
            self.endpoint["address"] = "192.0.2.11"
            self.imported["status"]["publicIngress"] = copy.deepcopy(self.ad["spec"]["publicIngress"])
            self.npa["spec"]["scope"]["endpoint"] = "192.0.2.11"
            authority.refresh_candidates()
            self.qualify()
            current = copy.deepcopy(authority.HEALTH["canary"]["canary-edge"])
            connection = mock.Mock()
            connection.getresponse.return_value = mock.Mock(status=200)
            with mock.patch.object(authority.socket, "create_connection"), \
                 mock.patch.object(authority.ssl, "create_default_context"), \
                 mock.patch.object(authority.http.client, "HTTPConnection", return_value=connection):
                authority.probe(old, "canary", "https://canary.example.com:8443/health")
            self.assertEqual(authority.HEALTH["canary"]["canary-edge"], current)

    def test_multiple_endpoints_add_remove_reorder_and_revision(self):
        with self.runtime():
            authority.refresh_candidates()
            original = self.qualify()["endpointRevision"]
            other = {**self.endpoint, "id": "site-b", "site": "site-b", "region": "region-b", "address": "192.0.2.22"}
            self.ad["spec"]["publicIngress"]["endpoints"].append(other)
            self.ad["spec"]["publicIngress"]["endpoints"].reverse()
            self.imported["status"]["publicIngress"] = copy.deepcopy(self.ad["spec"]["publicIngress"])
            authority.refresh_candidates()
            self.assertEqual(authority.CANDIDATES[0]["ip"], "192.0.2.10")
            self.assertEqual(authority.CANDIDATES[0]["endpointRevision"], original)
            self.assertIn("canary-edge", authority.HEALTH["canary"])
            self.endpoint["revision"] = "reviewed-2"
            self.imported["status"]["publicIngress"] = copy.deepcopy(self.ad["spec"]["publicIngress"])
            authority.refresh_candidates()
            self.assertNotIn("canary-edge", authority.HEALTH["canary"])
            self.ad["spec"]["publicIngress"]["endpoints"] = [other]
            self.imported["status"]["publicIngress"] = copy.deepcopy(self.ad["spec"]["publicIngress"])
            authority.refresh_candidates()
            self.assertFalse(authority.fabric_evidence(authority.CANDIDATES[0])["eligible"])
            self.assertEqual(authority.CANDIDATES[0]["federationEvidence"]["reason"], "FederationEndpointMissing")
