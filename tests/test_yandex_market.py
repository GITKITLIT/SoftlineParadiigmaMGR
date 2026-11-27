import json
import unittest

from pm.yandex_search.comparison import build_comparison_context
from test_yandex_comparison import ENVELOPE, LOCAL, OWN, WEB


def study(key, competitor, **fields):
    return LOCAL | {"id": key, "ref": f"Исследование · {key}", "competitor": competitor} | fields


def find(context, feature):
    return next(row for row in context["matrix"]["rows"] if row["feature"] == feature)


class MarketTests(unittest.TestCase):
    def test_multi_competitor_conditions_gaps_and_hypotheses(self):
        own = OWN | {"self_checkout": "Нет", "account": "Да", "consultation": "Да"}
        a = study("a", "Демо A", self_checkout="Да", account="Да", consultation="Линия 24/7")
        b = study("b", "Демо B", devices="5", price="300", self_checkout="не найдено")
        context = build_comparison_context(
            own_evidence=own, web_result=ENVELOPE | {"items": []}, local_evidence=[a, b],
            as_of="2026-11-15",
        )
        self.assertEqual(context["matrix"]["competitors"], ["Демо A", "Демо B"])
        prices = find(context, "price")["competitors"]
        self.assertEqual(prices["Демо A"]["comparison"]["status"], "advantage")
        self.assertEqual(prices["Демо B"]["comparison"]["different_terms"], ["devices"])
        checkout = find(context, "self_checkout")["competitors"]
        self.assertEqual(checkout["Демо A"]["comparison"]["status"], "disadvantage")
        self.assertEqual(checkout["Демо B"]["status"], "unknown")
        self.assertEqual(find(context, "account")["competitors"]["Демо A"]["comparison"]["status"], "match")
        self.assertEqual(find(context, "consultation")["competitors"]["Демо A"]["comparison"]["status"], "different")
        self.assertEqual(context["qwen_context"]["ux_hypotheses"][0]["kind"], "hypothesis")

    def test_dated_verified_conflicts_and_old_evidence_do_not_turn_into_claims(self):
        own = OWN | {"self_checkout": "Да", "support": "Да"}
        a = study("a", "Демо A", self_checkout="Да", support="Нет", study_date="2026-04-01")
        b = study("b", "Демо A", self_checkout="Да", support="Да", study_date="2026-11-02")
        c = study("c", "Демо A", self_checkout="Нет", study_date="2026-11-03")
        context = build_comparison_context(
            own_evidence=own, web_result=ENVELOPE | {"items": []}, local_evidence=[a, b, c],
            as_of="2026-11-27", max_sources=5,
        )
        checkout = find(context, "self_checkout")["competitors"]["Демо A"]
        self.assertEqual(checkout["comparison"]["status"], "conflict")
        self.assertEqual(checkout["evidence_ids"][:2], ["c", "b"])
        support = find(context, "support")["competitors"]["Демо A"]
        self.assertEqual(support["comparison"]["status"], "match")
        self.assertEqual(support["alternatives"][0]["status"], "stale")
        self.assertLessEqual(len(context["qwen_context"]["evidence"]), 5)
        ids = {x["id"] for x in context["qwen_context"]["evidence"]}
        for comparison in context["qwen_context"]["comparisons"]:
            self.assertLessEqual(set(comparison["evidence_ids"]), ids)

    def test_primary_price_and_snippet_provenance_with_explicit_competitor_alias(self):
        excerpt = WEB | {
            "id": "web:page", "content_type": "page_excerpt",
            "retrieved_at": "2026-11-03T09:00:00Z",
            "attributes": {key: OWN[key] for key in (
                "product", "version", "license_type", "duration", "devices", "currency", "tax",
            )} | {"price": "1300", "support": "Да"},
        }
        local = study("expired", "Демо A", price="999", study_date="2026-06-01")
        context = build_comparison_context(
            web_result=ENVELOPE | {"items": [WEB, excerpt]}, local_evidence=[local], own_evidence=OWN,
            as_of="2026-11-27", competitor_aliases={"example.com": "Демо A"},
        )
        price = find(context, "price")["competitors"]["Демо A"]
        self.assertEqual(price["scope"], "public_page")
        self.assertEqual(price["comparison"]["status"], "advantage")
        self.assertEqual({entry["status"] for entry in price["alternatives"]}, {"unverified", "stale"})
        self.assertEqual(context["evidence_index"]["web:page"]["as_of"], "2026-11-03T09:00:00Z")

    def test_compact_handoff_for_dense_sources(self):
        crowded = [study(f"a{i}", f"Демо {i % 8}", support=str(i), services="Пояснение " * 200,
                         text="Доказательство " * 200) for i in range(500)]
        context = build_comparison_context(
            web_result=ENVELOPE | {"items": []}, local_evidence=crowded,
            own_evidence=OWN | {"support": "Да", "services": "Да"},
            as_of="2026-11-27",
        )
        handoff = context["qwen_context"]
        self.assertEqual(len(context["matrix"]["competitors"]), 8)
        self.assertLess(len(json.dumps(handoff, ensure_ascii=False).encode("utf-8")), 16000)
        self.assertLessEqual(len(handoff["evidence"]), 24)

    def test_matrix_group_assembles_conditions_with_per_cell_provenance(self):
        fields = {key: OWN[key] for key in (
            "product", "version", "license_type", "duration", "devices", "currency", "tax",
        )}
        fields["price"] = "1300"
        rows = [
            {"id": key, "ref": f"Матрица · {key}", "kind": "Конкуренты", "text": str(value),
             "competitor": "Демо A", "offer_group": "matrix-A", "study_date": OWN["study_date"],
             key: value}
            for key, value in fields.items()
        ]
        context = build_comparison_context(
            web_result=ENVELOPE | {"items": []}, local_evidence=rows, own_evidence=OWN,
            as_of="2026-11-27",
        )
        price = find(context, "price")["competitors"]["Демо A"]
        self.assertEqual(price["comparison"]["status"], "advantage")
        self.assertEqual(price["fact_evidence_ids"], ["price"])
        self.assertEqual(set(price["condition_evidence_ids"]), fields.keys() - {"price"})
