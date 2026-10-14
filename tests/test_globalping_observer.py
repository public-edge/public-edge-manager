import os
import unittest
from unittest import mock

os.environ.setdefault("GLOBALPING_LOCATIONS_JSON", '[{"region":"Eastern Asia"}]')
os.environ.setdefault("GLOBALPING_DNS_TARGETS_JSON", '["example.com"]')
os.environ.setdefault("GLOBALPING_HTTP_TARGETS_JSON", '["api.example.com."]')

from public_edge_manager import globalping_observer as observer


class GlobalpingObserverTests(unittest.TestCase):
    def test_summarize_ping_reports_latency_and_loss(self):
        result = observer.summarize({
            "id": "measurement-1", "type": "ping",
            "results": [
                {"probe": {"country": "JP", "region": "Eastern Asia"},
                 "result": {"status": "finished", "stats": {"avg": 18.5, "loss": 0}}},
                {"probe": {"country": "DE", "region": "Western Europe"},
                 "result": {"status": "finished", "stats": {"avg": 204.5, "loss": 25}}},
            ],
        })
        self.assertTrue(result["ready"])
        self.assertEqual(result["latencyAvgMs"], 111.5)
        self.assertEqual(result["latencyMaxMs"], 204.5)
        self.assertEqual(result["lossMaxPercent"], 25)

    def test_summarize_http_rejects_server_error(self):
        result = observer.summarize({
            "id": "measurement-2", "type": "http",
            "results": [{"probe": {"country": "US", "region": "Northern America"},
                         "result": {"status": "finished", "statusCode": 503,
                                    "timings": {"total": 320}}}],
        })
        self.assertFalse(result["ready"])
        self.assertEqual(result["samples"][0]["latencyMs"], 320)

    @mock.patch.object(observer, "publish_edge_quality")
    @mock.patch.object(observer, "publish_configmap")
    @mock.patch.object(observer, "measure")
    @mock.patch.object(observer, "public_edges")
    def test_observe_once_covers_candidate_dns_and_service_layers(
            self, edges, measure, publish_configmap, publish_edge_quality):
        edges.return_value = [{"name": "edge-a", "address": "192.0.2.10"}]
        measure.return_value = {"ready": True, "measurementId": "m"}
        with mock.patch.object(observer, "DNS_TARGETS", ["example.com"]), \
                mock.patch.object(observer, "HTTP_TARGETS", ["api.example.com."]), \
                mock.patch.object(observer, "EXTRA_AUTHORITIES", ["192.0.2.53"]):
            document = observer.observe_once()
        self.assertEqual(set(document), {"provider", "observedAt", "locations",
                                         "candidates", "authorities", "services"})
        self.assertEqual(len(document["candidates"]), 1)
        self.assertEqual(len(document["authorities"]), 2)
        self.assertIn("api.example.com", document["services"])
        publish_configmap.assert_called_once()
        publish_edge_quality.assert_called_once()

    @mock.patch.object(observer, "publish_edge_quality")
    @mock.patch.object(observer, "publish_configmap")
    @mock.patch.object(observer, "measure")
    @mock.patch.object(observer, "public_edges")
    def test_observe_once_enforces_cycle_measurement_budget(
            self, edges, measure, publish_configmap, publish_edge_quality):
        edges.return_value = [
            {"name": f"edge-{index}", "address": f"192.0.2.{index}"}
            for index in range(1, 10)
        ]
        measure.return_value = {"ready": True, "measurementId": "m"}
        with mock.patch.object(observer, "MAX_MEASUREMENTS", 3), \
                mock.patch.object(observer, "DNS_TARGETS", ["example.com"]), \
                mock.patch.object(observer, "HTTP_TARGETS", ["api.example.com."]), \
                mock.patch.object(observer, "EXTRA_AUTHORITIES", ["192.0.2.53"]):
            document = observer.observe_once()
        self.assertEqual(measure.call_count, 3)
        self.assertEqual(len(document["candidates"]), 1)
        self.assertEqual(len(document["authorities"]), 1)
        self.assertEqual(len(document["services"]), 1)


if __name__ == "__main__":
    unittest.main()
