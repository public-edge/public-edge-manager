import time
import unittest
from unittest import mock

from public_edge_manager import authority


class RegionalPreferenceTests(unittest.TestCase):
    def setUp(self):
        policy = {'regions': {'EU': [
            {'id': 'preferred-policy', 'ip': '192.0.2.1'},
            {'id': 'fallback', 'ip': '192.0.2.2'}]}}
        for name, value in [('ROUTING_POLICY', policy), ('SELECTIONS', {}),
                            ('CANDIDATE_MIN_READY_SECONDS', 20),
                            ('CANDIDATE_MIN_HOLD_SECONDS', 60)]:
            patch = mock.patch.object(authority, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.primary = {'id': 'discovered-primary', 'ip': '192.0.2.1',
                        'state': 'ready', 'serviceBackendReady': True}
        self.fallback = {'id': 'fallback', 'ip': '192.0.2.2',
                         'state': 'ready', 'serviceBackendReady': True}

    def select(self, candidates, timestamp):
        return authority.stable_selected('app', candidates, 'EU', timestamp)['id']

    def test_startup_fallback_returns_to_qualified_explicit_primary(self):
        self.assertEqual(self.select([self.fallback], 0), 'fallback')
        self.assertEqual(self.select([self.primary, self.fallback], 100), 'fallback')
        self.assertEqual(self.select([self.primary, self.fallback], 119), 'fallback')
        self.assertEqual(self.select([self.primary, self.fallback], 120), 'discovered-primary')

    def test_flapping_preference_restarts_qualification(self):
        self.select([self.fallback], 0)
        self.select([self.primary, self.fallback], 100)
        self.select([dict(self.primary, serviceBackendReady=False), self.fallback], 110)
        self.assertEqual(self.select([self.primary, self.fallback], 120), 'fallback')
        self.assertEqual(self.select([self.primary, self.fallback], 139), 'fallback')
        self.assertEqual(self.select([self.primary, self.fallback], 140), 'discovered-primary')

    def test_failback_respects_current_minimum_hold(self):
        self.select([self.fallback], 0)
        self.select([self.primary, self.fallback], 1)
        self.assertEqual(self.select([self.primary, self.fallback], 21), 'fallback')
        self.assertEqual(self.select([self.primary, self.fallback], 60), 'discovered-primary')

    def test_declared_order_beats_capacity_and_measurement_scores(self):
        candidates = [{**item, 'region': 'site', 'area': 'EU',
                       'capacityMbps': capacity, 'probes': {'app': 'https://example.com'}}
                      for item, capacity in [(self.primary, 10), (self.fallback, 1000)]]
        health = {'app': {item['id']: {'ready': True, 'observedAt': int(time.time()),
                                     'latencyMs': 1} for item in candidates}}
        with mock.patch.object(authority, 'CANDIDATES', candidates), \
             mock.patch.object(authority, 'HEALTH', health), \
             mock.patch.object(authority, 'readiness_gate_ready', return_value=True), \
             mock.patch.object(authority, 'fabric_evidence', return_value={'eligible': True}):
            result = authority.ranked('app', 'EU')
        self.assertGreater(result[1]['score'], result[0]['score'])
        self.assertEqual(result[0]['id'], 'discovered-primary')
