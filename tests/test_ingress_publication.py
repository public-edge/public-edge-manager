import copy
import json
import subprocess
from pathlib import Path

import yaml

from public_edge_manager import ingress_publication as publication

NOW = 1800000000
SOURCE = {'namespace': 'apps', 'name': 'backend', 'bindings': [
    {'id': 'tools', 'service': 'tools', 'hostname': 'tools.example.com', 'path': '/ready'}]}


def edge(name='edge-one', address='8.8.8.8'):
    return {'metadata': {'name': name, 'uid': name + '-uid', 'generation': 3,
                         'labels': {'networking.re8ch.com/managed': 'true',
                                    'networking.re8ch.com/node-uid': 'node-uid'}},
            'spec': {'enabled': True, 'draining': False, 'nodeName': name, 'region': 'eu-central',
                     'endpoint': {'value': address}, 'ports': [443],
                     'discovery': {'source': 'Node+NetworkPathAssessment+Gateway', 'nodeUID': 'node-uid'}},
            'status': {'observedGeneration': 3, 'conditions': [{'type': 'Ready', 'status': 'True'}],
                       'services': {'tools': {'ready': True, 'observedAt': NOW}}}}


def test_multiple_local_nodes_keep_stable_ids_and_change_revision_on_ip_or_service_recreation():
    original = [edge(), edge('edge-two', '1.1.1.1')]
    first = publication.endpoints(SOURCE, original, 'main', 'service-uid', NOW, 30)
    assert len(first) == 2
    assert first == publication.endpoints(SOURCE, list(reversed(original)), 'main', 'service-uid', NOW + 1, 30)
    original[0]['spec']['endpoint']['value'] = '9.9.9.9'
    moved = publication.endpoints(SOURCE, original, 'main', 'service-uid', NOW, 30)
    assert moved[0]['id'] == first[0]['id'] and moved[0]['revision'] != first[0]['revision']
    assert moved[1] == first[1]
    replaced = publication.endpoints(SOURCE, original, 'main', 'new-service-uid', NOW, 30)
    assert replaced[0]['revision'] != moved[0]['revision']


def test_foreign_unbound_stale_and_unready_edges_are_not_claimed_by_local_owner():
    candidates = []
    for mutation in (
        lambda e: e['metadata']['labels'].update({'networking.re8ch.com/federated': 'true'}),
        lambda e: e['spec']['discovery'].update({'source': 'AmongCluster'}),
        lambda e: e['status']['services']['tools'].update({'observedAt': NOW - 31}),
        lambda e: e['status']['services']['tools'].update({'ready': False}),
        lambda e: e['status'].update({'observedGeneration': 2}),
        lambda e: e['spec'].update({'draining': True}),
        lambda e: e['metadata'].update({'deletionTimestamp': 'now'}),
        lambda e: e['metadata']['labels'].update({'networking.re8ch.com/node-uid': 'wrong-node'}),
    ):
        candidate = edge(); mutation(candidate); candidates.append(candidate)
    assert publication.endpoints(SOURCE, candidates, 'main', 'service-uid', NOW, 30) == []


def test_source_heartbeat_does_not_change_ingress_and_withdrawal_removes_the_annotation():
    service = {'metadata': {'uid': 'service-uid', 'resourceVersion': 'rv', 'annotations': {}}}
    writes = []
    get = lambda _: copy.deepcopy(service)
    patch = lambda path, value: writes.append((path, value))
    publication.publish([SOURCE], [edge()], 'main', NOW, 30, 90, get, patch)
    publication.publish([SOURCE], [edge()], 'main', NOW + 1, 30, 90, get, patch)
    first, second = [value['metadata']['annotations'] for _, value in writes]
    assert first[publication.ANNOTATION] == second[publication.ANNOTATION]
    assert first[publication.VALID_UNTIL] != second[publication.VALID_UNTIL]
    assert first[publication.SOURCE_UID] == 'service-uid'
    assert writes[0][1]['metadata']['resourceVersion'] == 'rv'
    publication.publish([SOURCE], [], 'main', NOW + 2, 30, 90, get, patch)
    assert writes[-1][1]['metadata']['annotations'][publication.ANNOTATION] is None


def test_one_failed_service_does_not_stop_unrelated_withdrawal_or_renew_its_source_lease():
    writes = []
    def get(path):
        if path.endswith('/backend'):
            raise PermissionError('fixture denied')
        return {'metadata': {'uid': 'other-uid', 'resourceVersion': 'rv'}}
    other = {**SOURCE, 'name': 'other'}
    result = publication.publish([SOURCE, other], [], 'main', NOW, 30, 90,
                                 get, lambda path, value: writes.append((path, value)))
    assert [row['state'] for row in result] == ['Error', 'Withdrawn']
    assert len(writes) == 1 and writes[0][0].endswith('/other')


def test_chart_grants_only_named_service_get_and_patch():
    root = Path(__file__).resolve().parents[1]
    values = {'enabled': True, 'discovery': {'enabled': True},
              'services': {'tools.example.com.': {'service': 'tools', 'class': 'web', 'probePath': '/ready'}},
              'federation': {'clusterRef': 'main', 'sourceServices': [SOURCE]}}
    output = subprocess.run(['helm', 'template', 'fixture', str(root), '--namespace', 'routing', '-f', '-'],
                            input=yaml.safe_dump(values), text=True, capture_output=True, check=True).stdout
    objects = [obj for obj in yaml.safe_load_all(output) if obj]
    role = next(obj for obj in objects if obj['kind'] == 'Role' and '-ingress-' in obj['metadata']['name'])
    assert role['metadata']['namespace'] == 'apps'
    assert role['rules'] == [{'apiGroups': [''], 'resources': ['services'],
                              'resourceNames': ['backend'], 'verbs': ['get', 'patch']}]
    binding = next(obj for obj in objects if obj['kind'] == 'RoleBinding' and obj['metadata']['name'] == role['metadata']['name'])
    assert binding['subjects'][0]['namespace'] == 'routing'
    controller = next(obj for obj in objects if obj['kind'] == 'Deployment' and obj['metadata']['name'].endswith('-controller'))
    env = controller['spec']['template']['spec']['containers'][0]['env']
    assert json.loads(next(v['value'] for v in env if v['name'] == 'LOCAL_INGRESS_SOURCES_JSON')) == [SOURCE]
