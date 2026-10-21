import unittest

from public_edge_manager.selection import ScoreModel, score_candidate, stable_rank


class SelectionModelTests(unittest.TestCase):
    def test_declared_formula_and_components(self):
        score = score_candidate(
            capacity_mbps=300, candidate_area="a", request_area="a",
            priority=10, latency_ms=50, candidate_id="node-a",
            local_node_id="node-a",
        )
        self.assertEqual(score.capacity, 3000)
        self.assertEqual(score.locality, 100000)
        self.assertEqual(score.priority, -10)
        self.assertEqual(score.latency, -2)
        self.assertEqual(score.local_node, 5)
        self.assertEqual(score.total, 102993)

    def test_latency_penalty_uses_twenty_ms_steps_and_caps(self):
        base = dict(capacity_mbps=100, candidate_area="a", request_area="b",
                    priority=0, candidate_id="n", local_node_id="other")
        self.assertEqual(score_candidate(latency_ms=19, **base).latency, 0)
        self.assertEqual(score_candidate(latency_ms=20, **base).latency, -1)
        self.assertEqual(score_candidate(latency_ms=1000, **base).latency, -50)
        self.assertEqual(score_candidate(latency_ms=5000, **base).latency, -50)

    def test_area_bonus_is_a_hard_declared_component(self):
        base = dict(capacity_mbps=50, priority=0, latency_ms=0,
                    candidate_id="n", local_node_id="other")
        local = score_candidate(candidate_area="a", request_area="a", **base)
        remote = score_candidate(candidate_area="b", request_area="a", **base)
        self.assertEqual(local.total - remote.total, 100000)

    def test_ties_sort_by_node_id(self):
        ranked = stable_rank([{"id": "node-b", "score": 10}, {"id": "node-a", "score": 10}])
        self.assertEqual([item["id"] for item in ranked], ["node-a", "node-b"])

    def test_custom_model_keeps_the_function_pure(self):
        model = ScoreModel(capacity_weight=1, local_area_bonus=2, priority_weight=3,
                           latency_divisor_ms=4, latency_penalty_cap=5, local_node_bonus=6)
        score = score_candidate(capacity_mbps=7, candidate_area="a", request_area="a",
                                priority=1, latency_ms=40, candidate_id="n",
                                local_node_id="n", model=model)
        self.assertEqual(score.total, 7 + 2 - 3 - 5 + 6)


if __name__ == "__main__":
    unittest.main()
