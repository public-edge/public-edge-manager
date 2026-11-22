"""Consume explicitly bound AmongCluster advertisements; never convert QUIC URLs."""
import ipaddress
import re
from datetime import datetime


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def resolve(discovery, get, now):
    """Return a fresh, identity-bound public ingress or a fail-closed decision."""
    decision = {"eligible": False, "reason": "FederationReferenceInvalid"}
    ad_ref, import_ref = discovery.get("advertisementRef", {}), discovery.get("importedServiceRef", {})
    if not all(isinstance(ref, dict) and all(ref.get(key) for key in ("namespace", "name", "uid"))
               for ref in (ad_ref, import_ref)) or ad_ref["namespace"] != import_ref["namespace"]:
        return decision
    if any(not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", ref[key])
           for ref in (ad_ref, import_ref) for key in ("namespace", "name")):
        return decision
    base = "/apis/peering.re8ch.com/v1alpha1/namespaces/" + ad_ref["namespace"]
    ad = get(base + "/serviceadvertisements/" + ad_ref["name"])
    imported = get(base + "/importedservices/" + import_ref["name"])
    if not ad or not imported:
        return {**decision, "reason": "FederationUnavailable"}
    meta, spec = ad.get("metadata", {}), ad.get("spec", {})
    imeta, ispec, istatus = imported.get("metadata", {}), imported.get("spec", {}), imported.get("status", {})
    decision.update({"advertisementRef": {**ad_ref, "generation": meta.get("generation"),
                     "resourceVersion": meta.get("resourceVersion")},
                     "importedServiceRef": {**import_ref, "generation": imeta.get("generation"),
                     "resourceVersion": imeta.get("resourceVersion")}})
    if meta.get("deletionTimestamp") or imeta.get("deletionTimestamp"):
        return {**decision, "reason": "FederationDeleting"}
    if meta.get("uid") != ad_ref["uid"] or imeta.get("uid") != import_ref["uid"]:
        return {**decision, "reason": "FederationUIDMismatch"}
    if (spec.get("publisherRef") != discovery.get("clusterRef") or
            spec.get("serviceIdentity") != discovery.get("serviceRef") or
            spec.get("protocol") != discovery.get("protocol") or
            not discovery.get("protocol") or ispec.get("advertisementRef") != ad_ref["name"] or
            ispec.get("peerRef") not in spec.get("targetPeers", [])):
        return {**decision, "reason": "FederationIdentityMismatch"}
    published = timestamp(meta.get("annotations", {}).get("peering.re8ch.com/last-published-at"))
    ttl = spec.get("ttlSeconds", 0)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 30 <= ttl <= 86400 or not 0 < published <= now:
        return {**decision, "reason": "FederationValidityInvalid"}
    decision["expiresAt"] = published + ttl
    if spec.get("revoked") or now >= decision["expiresAt"]:
        return {**decision, "reason": "FederationRevoked" if spec.get("revoked") else "FederationExpired"}
    if (istatus.get("state") != "Ready" or ispec.get("suspended") or
            istatus.get("advertisementUID") != meta.get("uid") or
            istatus.get("advertisementGeneration") != meta.get("generation") or
            istatus.get("observedGeneration") != imeta.get("generation")):
        return {**decision, "reason": "FederationImportNotObserved"}
    ingress = spec.get("publicIngress", {})
    try:
        address = ipaddress.ip_address(ingress["address"])
        port = ingress["port"]
        if (ingress["protocol"] != "https" or address.version != 4 or address.is_loopback or
                address.is_unspecified or address.is_multicast or address.is_link_local or
                not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 or
                not re.fullmatch(r"(?:[A-Za-z0-9](?:[-A-Za-z0-9]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[-A-Za-z0-9]{0,61}[A-Za-z0-9])?", ingress["hostname"]) or
                len(ingress["hostname"]) > 253 or not ingress["path"].startswith("/") or
                ingress["path"].startswith("//") or len(ingress["path"]) > 2048 or
                not ingress["revision"] or len(ingress["revision"]) > 128 or
                istatus.get("publicIngress") != ingress):
            raise ValueError("invalid ingress")
    except (KeyError, TypeError, ValueError):
        return {**decision, "reason": "FederationIngressInvalid"}
    return {**decision, "eligible": True, "reason": "FederationObserved",
            "publicIngress": ingress, "serviceIdentity": spec["serviceIdentity"],
            "publisherRef": spec["publisherRef"]}


def assessment_reference(discovery, source_node):
    refs = discovery.get("assessmentRefs")
    ref = refs.get(source_node, {}) if isinstance(refs, dict) else discovery.get("assessmentRef", {})
    return ref if isinstance(ref, dict) and ref.get("sourceNode") == source_node else {}


def service_assessment(discovery, decision, get, group, version, source_node):
    """Fetch one explicitly named Service NPA at refresh, never at DNS request time."""
    ref = assessment_reference(discovery, source_node)
    required = ("name", "uid", "sourceNode", "targetId", "paddingBytes")
    if (not isinstance(ref, dict) or not all(ref.get(key) is not None for key in required) or
            not all(isinstance(ref[key], str) and ref[key] for key in required[:-1]) or
            not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", ref["name"])):
        return None
    return get(f"/apis/{group}/{version}/networkpathassessments/{ref['name']}")


def assess_service(discovery, decision, item, now, source_node):
    """Validate service/endpoint/source/size scope and freshness, including large TLS transfer."""
    rejected = {"eligible": False, "mode": "Required", "reason": "ServiceAssessmentMismatch",
                "state": "Unavailable", "federation": decision, "pathEvidence": {}}
    if not item:
        return {**rejected, "reason": "ServiceAssessmentUnavailable"}
    ref = assessment_reference(discovery, source_node)
    meta, spec, status = item.get("metadata", {}), item.get("spec", {}), item.get("status", {})
    ingress = decision.get("publicIngress", {})
    subject, scope = spec.get("subjectRef", {}), spec.get("scope", {})
    identity = re.fullmatch(r"spiffe://[^/]+/ns/([^/]+)/service/([^/]+)", discovery.get("serviceRef", ""))
    sizes, expected = scope.get("paddingBytes"), ref.get("paddingBytes")
    if (not identity or meta.get("name") != ref.get("name") or meta.get("uid") != ref.get("uid") or
            meta.get("deletionTimestamp") or subject.get("apiVersion") != "v1" or
            subject.get("kind") != "Service" or (subject.get("namespace"), subject.get("name")) != identity.groups() or
            scope.get("plane") != "host-and-pod" or scope.get("protocol") != "tcp" or
            scope.get("direction") != "bidirectional" or
            scope.get("sourceNode") != ref.get("sourceNode") or scope.get("targetId") != ref.get("targetId") or
            not isinstance(expected, list) or not expected or
            any(not isinstance(size, int) or isinstance(size, bool) or size < 0 for size in expected) or
            not any(size >= 2048 for size in expected) or sizes != expected or
            (scope.get("endpoint"), scope.get("port"), scope.get("serverName"), scope.get("path")) !=
            (ingress.get("address"), ingress.get("port"), ingress.get("hostname"), ingress.get("path"))):
        return rejected
    expected_digest = ref.get("expectedConfigurationDigest")
    if expected_digest is not None and (not isinstance(expected_digest, str) or not expected_digest or
                                         scope.get("targetConfigurationDigest") != expected_digest):
        return {**rejected, "reason": "ServiceConfigurationDigestMismatch"}
    observed, valid = timestamp(status.get("observedAt")), timestamp(status.get("validUntil"))
    condition = next((value for value in status.get("conditions", []) if value.get("type") == "EvidenceReady"), {})
    service = status.get("serviceEvidence", {})
    path = status.get("pathEvidence", {})
    ready = (status.get("state") in ("Ready", "Partial") and
             0 < observed <= now < valid and condition.get("status") == "True" and
             service.get("definition") == "tls-transfer-v1" and service.get("required") is True and
             service.get("complete") is True and service.get("withinLatencyBudget") is True and
             isinstance(service.get("maximumTotalMs"), (int, float)) and
             not isinstance(service.get("maximumTotalMs"), bool) and service["maximumTotalMs"] > 0 and
             path.get("currentPathMeasured") is True and path.get("reachable") is True)
    return {**rejected, "eligible": ready, "wouldReject": not ready,
            "reason": "ServiceTransferEvidenceReady" if ready else "ServiceTransferEvidenceRejected",
            "state": status.get("state", "Unknown"), "observedAt": status.get("observedAt"),
            "validUntil": status.get("validUntil"), "pathEvidence": path,
            "assessment": meta.get("name"), "assessmentRef": {
                **{key: meta[key] for key in ("name", "uid", "generation", "resourceVersion") if key in meta},
                "subjectRef": subject, "scope": scope}, "serviceEvidence": service}
