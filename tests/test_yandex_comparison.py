import unittest

from pm.yandex_search.comparison import build_comparison_context
from scripts.search_investigation_smoke import run


TERMS = {
    "product": "Демо Pro", "version": "2", "license_type": "Стандарт",
    "duration": "12 месяцев", "devices": "1", "currency": "RUB", "tax": "включён",
    "study_date": "2026-11-03",
}
OWN = TERMS | {"price": "900,00", "id": "own", "ref": "Демо · тариф", "kind": "Каталог"}
LOCAL = TERMS | {
    "price": "1 050,50", "id": "local", "ref": "Синтетика · A1:C1", "kind": "Конкуренты",
    "text": "Цена и условия", "consultation": "Доступна консультация",
}
WEB = {
    "id": "web:demo", "ref": "https://example.com/offer", "kind": "competitor_web",
    "domain": "example.com", "content_type": "snippet", "product": "Демо Pro",
    "retrieved_at": "2026-11-04T00:00:00Z", "published_at": None,
    "text": ("Продукт: Демо Pro; Версия: 2; Тип лицензии: Стандарт; Срок: 12 месяцев;"
             "Устройства: 1; Валюта: руб.; НДС: с НДС; Цена: 1000;"
             "Дата проверки: 2026-11-03; Путь покупки: выбор лицензии перед корзиной"),
}
ENVELOPE = {
    "items": [WEB], "status": "ok", "reason": None, "retrieved_at": WEB["retrieved_at"],
    "warnings": [],
}


class ComparisonTests(unittest.TestCase):
    def context(self, own=OWN, local=LOCAL, web=WEB):
        return build_comparison_context(
            own_evidence=own, local_evidence=[local], web_result=ENVELOPE | {"items": [web]},
            as_of="2026-11-27",
        )

    def test_compares_matching_dated_terms_and_marks_snippet_unverified(self):
        context = self.context()
        local, web = context["observations"]
        self.assertEqual(context["own"]["source"]["scope"], "internal_offer")
        self.assertEqual(local["source"]["scope"], "local_study")
        self.assertEqual(local["source"]["study_date"], "2026-11-03")
        self.assertEqual(local["ux"]["consultation"], "Доступна консультация")
        self.assertEqual(local["price_comparison"]["status"], "comparable")
        self.assertEqual(local["price_comparison"]["competitor_minus_own"], {
            "amount": "150.50", "currency": "RUB",
        })
        self.assertEqual(web["source"]["scope"], "search_snippet")
        self.assertEqual(web["source"]["retrieved_at"], WEB["retrieved_at"])
        self.assertEqual(web["ux"]["purchase_flow"], "выбор лицензии перед корзиной")
        self.assertEqual(web["price_comparison"]["status"], "requires_verification")
        self.assertIsNone(web["price_comparison"]["competitor_minus_own"])
        self.assertIsNone(web["hypothesis"])

    def test_unknown_or_different_terms_and_dates_block_delta(self):
        for overrides, status, term in (
            ({"devices": "5"}, "mismatched_terms", "devices"),
            ({"tax": "без НДС"}, "insufficient_data", "tax"),
            ({"version": "n/a"}, "insufficient_data", "version"),
            ({"study_date": "2026-11-05"}, "requires_verification", "different_days"),
            ({"study_date": None}, "requires_verification", "unknown"),
        ):
            result = self.context(local=LOCAL | overrides)["observations"][0]["price_comparison"]
            with self.subTest(overrides=overrides):
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["competitor_minus_own"])
                if status == "mismatched_terms":
                    self.assertIn(term, result["different_terms"])
                if status == "insufficient_data":
                    self.assertIn(term, result["missing"]["competitor"])
                if status == "requires_verification":
                    self.assertEqual(term, result["date_alignment"])

    def test_only_internal_own_data_and_no_query_product_as_offer_fact(self):
        with self.assertRaises(ValueError):
            self.context(own=WEB)
        result = self.context(own=None, web=WEB | {"text": "Цена: 555"})
        self.assertIsNone(result["own"])
        web = result["observations"][1]
        self.assertEqual(web["offer"]["price"], "555")
        self.assertIsNone(web["offer"]["product"])
        self.assertIn("product", web["price_comparison"]["missing"]["competitor"])

    def test_conflicting_labels_cannot_supply_a_price(self):
        context = self.context(web=WEB | {"text": WEB["text"] + "; Цена: 999"})
        observation = context["observations"][1]
        self.assertIsNone(observation["offer"]["price"])
        self.assertIn("price", observation["price_comparison"]["missing"]["competitor"])

    def test_offline_combined_investigation(self):
        result = run()
        self.assertEqual(result["mode"], "offline")
        self.assertEqual(result["backlog_id"], "TASK-02")
        self.assertIn("checkout", result["checks"])
        self.assertEqual(result["source_scopes"], ["local_study", "search_snippet"])
