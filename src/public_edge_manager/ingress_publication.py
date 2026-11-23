"""Project local, observed ingress capabilities into existing Service exports.

AmongCluster remains the publisher and federation authority. This module owns
only one annotation on explicitly configured Services, never remote Nodes,
Service routing, grants, DNS records or another cluster's advertisements.
"""
import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone

ANNOTATION = 'peering.re8ch.com/public-ingress'
OWNER = 'networking.re8ch.com/public-ingress-owner'
SOURCE_UID = 'networking.re8ch.com/public-ingress-source-uid'
VALID_UNTIL = 'networking.re8ch.com/public-ingress-valid-until'


def endpoints(source, edges, cluster_ref, service_uid, now, max_age):
    result = []
    for edge in edges:
        meta, spec, status = (edge.get(key, {}) for key in ('metadata', 'spec', 'status'))
        labels = meta.get('labels', {})
        if (meta.get('deletionTimestamp') or labels.get('networking.re8ch.com/managed') != 'true'
                or labels.get('networking.re8ch.com/federated') == 'true'
                or spec.get('discovery', {}).get('source') != 'Node+NetworkPathAssessment+Gateway'
                or not spec.get('enabled') or spec.get('draining')
                or status.get('observedGeneration') != meta.get('generation')
                or not any(c.get('type') == 'Ready' and c.get('status') == 'True'
                           for c in status.get('conditions', []))):
            continue
        node_uid = spec.get('discovery', {}).get('nodeUID')
        if not node_uid or labels.get('networking.re8ch.com/node-uid') != node_uid:
            continue
        address = spec.get('endpoint', {}).get('value', '')
        try:
            ip = ipaddress.ip_address(address)
            if ip.version != 4 or not ip.is_global:
                continue
        except ValueError:
            continue
        for binding in source['bindings']:
            observed = status.get('services', {}).get(binding['service'], {})
            stamp = observed.get('observedAt', 0)
            if (observed.get('ready') is not True or not isinstance(stamp, (int, float))
                    or isinstance(stamp, bool) or not 0 <= now - stamp <= max_age):
                continue
            endpoint = {'id': binding['id'] + '-' + meta['name'],
                        'site': spec.get('nodeName', ''), 'region': spec.get('region', ''),
                        'protocol': 'https', 'address': str(ip), 'port': binding.get('port', 443),
                        'hostname': binding['hostname'], 'path': binding['path']}
            if (len(endpoint['id']) > 63 or not endpoint['site'] or not endpoint['region']
                    or endpoint['region'] == 'unknown' or endpoint['port'] not in spec.get('ports', [])):
                continue
            # Stable across routine probes; changes on address, Service identity,
            # local edge identity, binding or gateway specification changes.
            revision = {'clusterRef': cluster_ref, 'serviceUID': service_uid,
                        'edgeUID': meta.get('uid'), 'edgeSpec': spec, 'endpoint': endpoint}
            endpoint['revision'] = hashlib.sha256(json.dumps(revision, sort_keys=True,
                                                            separators=(',', ':')).encode()).hexdigest()
            result.append(endpoint)
    result.sort(key=lambda value: value['id'])
    if len(result) > 32 or len({value['id'] for value in result}) != len(result):
        raise ValueError('local ingress exceeds the Among endpoint contract or has duplicate IDs')
    return result


def publish(sources, edges, cluster_ref, now, max_age, validity, get, patch):
    if not sources:
        return []
    if not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', cluster_ref):
        raise ValueError('local ingress publication requires the owner cluster reference')
    outcomes = []
    for source in sources:
        path = '/api/v1/namespaces/' + source['namespace'] + '/services/' + source['name']
        try:
            service = get(path)
            meta = service['metadata']
            if meta.get('deletionTimestamp'):
                outcomes.append({'service': source['namespace'] + '/' + source['name'], 'state': 'Deleting'})
                continue
            annotations = meta.get('annotations', {})
            if annotations.get(OWNER, cluster_ref) != cluster_ref:
                raise ValueError('Service ingress publication belongs to another cluster')
            selected = endpoints(source, edges, cluster_ref, meta['uid'], now, max_age)
            value = json.dumps({'endpoints': selected}, sort_keys=True, separators=(',', ':')) if selected else None
            expires = datetime.fromtimestamp(now + validity, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
            # The agent checks this source heartbeat before renewing public
            # ingress. It is not copied into the advertisement specification,
            # so ordinary heartbeats do not invalidate consumer generations.
            patch(path, {'metadata': {'uid': meta['uid'], 'resourceVersion': meta['resourceVersion'],
                                     'annotations': {ANNOTATION: value, OWNER: cluster_ref,
                                                     SOURCE_UID: meta['uid'], VALID_UNTIL: expires}}})
            outcomes.append({'service': source['namespace'] + '/' + source['name'],
                             'state': 'Published' if selected else 'Withdrawn', 'endpoints': len(selected)})
        except Exception as error:
            # A projection failure must neither stop DNS reconciliation nor
            # prevent unrelated Services from withdrawing. Old publication is
            # bounded by the existing source validity rather than renewed here.
            outcomes.append({'service': source['namespace'] + '/' + source['name'],
                             'state': 'Error', 'reason': type(error).__name__})
    return outcomes
