"""Регрессии повторных поступлений. Без сети и настоящих сообщений Telegram."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor as m


def card(pid="16688", sold=False, price="15 000 ₽", image="a.webp"):
    return {"id": pid, "title": "Бродский, И.А. Часть речи", "sold": sold,
            "price": None if sold else price, "url": "https://abelbooks.ru/product/test/",
            "images": ["https://abelbooks.ru/" + image] if image else []}


def observe(items, state=None, books=None, day="2026-10-26"):
    return m.observe_abel(items, state, books or {}, today=day, observed_at=day + "T12:00:00+00:00")


class LifecycleTests(unittest.TestCase):
    def test_upgrade_seeds_silently_even_with_legacy_ids(self):
        state = observe([card(), card("2", sold=True)])
        self.assertEqual(state["pending"], [])
        self.assertEqual(len(state["items"]), 2)

    def test_two_returns_and_sales_with_same_id_and_different_prices(self):
        state = observe([card(sold=True)])
        state = observe([card(price="15 000 ₽")], state)
        state = observe([card(sold=True)], state, day="2026-10-27")
        state = observe([card(price="25 000 ₽", image="b.webp")], state, day="2026-10-28")
        state = observe([card(sold=True, image="b.webp")], state, day="2026-10-29")
        events = [e["item"] for e in state["pending"]]
        self.assertEqual([e["tag"] for e in events], ["Абель – снова в продаже", "Абель – продано"] * 2)
        self.assertEqual([e["price"] for e in events], ["15 000 ₽"] * 2 + ["25 000 ₽"] * 2)
        self.assertEqual(events[-1]["first_seen"], "2026-10-28")
        self.assertEqual(len(state["items"]["16688"]["history"]), 5)
        self.assertEqual(state["items"]["16688"]["cycle"], 2)

    def test_unchanged_snapshot_does_not_repeat_events(self):
        state = observe([card(sold=True)])
        state = observe([card()], state)
        again = observe([card()], state, day="2026-10-27")
        self.assertEqual(again["pending"], state["pending"])
        self.assertEqual(again["items"]["16688"]["history"], state["items"]["16688"]["history"])

    def test_price_and_photo_update_without_id_change(self):
        state = observe([card()])
        state = observe([card(price="30 000 ₽", image="b.webp")], state)
        event = state["pending"][0]["item"]
        self.assertEqual(event["tag"], "Абель – изменилась цена и фото")
        self.assertIn("раньше 15 000 ₽", m.format_message(event))

    def test_return_without_price_never_uses_previous_copy_price(self):
        state = observe([card()])
        state = observe([card(sold=True)], state)
        state = observe([card(price=None)], state)
        self.assertIsNone(state["pending"][-1]["item"]["price"])
        state = observe([card(sold=True)], state)
        text = m.format_message(state["pending"][-1]["item"])
        self.assertIn("Цена не сохранена", text)
        self.assertNotIn("15 000", text)

    def test_new_unknown_sale_explicitly_reports_missing_price(self):
        state = observe([card()])
        state = observe([card(), card("2", sold=True)], state)
        self.assertIn("Цена не сохранена", m.format_message(state["pending"][0]["item"]))

    def test_legacy_price_and_real_sale_dates_survive_baseline(self):
        books = {"16688": {"price": "45 000 ₽", "status": "sold", "first_seen": "2026-07-04 – стали трекать историю",
                           "sold_date": "2026-10-01"}}
        state = observe([card(sold=True)], books=books)
        item = state["items"]["16688"]
        self.assertEqual(item["price"], "45 000 ₽")
        self.assertEqual(item["sold_date"], "2026-10-01")
        self.assertTrue(item["cycle_started"].startswith("2026-07-04"))

    def test_absent_id_is_not_assumed_sold(self):
        state = observe([card(), card("2")])
        state = observe([card("2")], state)
        self.assertEqual(state["items"]["16688"]["status"], "instock")
        self.assertEqual(state["pending"], [])

    def test_empty_snapshot_fails_without_touching_previous(self):
        state = observe([card()])
        original = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            observe([], state)
        self.assertEqual(state, original)

    def test_missing_photo_does_not_generate_photo_change(self):
        state = observe([card()])
        state = observe([card(image="")], state)
        self.assertEqual(state["pending"], [])
        state = observe([card(image="b.webp")], state)
        self.assertEqual(state["pending"][0]["item"]["tag"], "Абель – обновлено фото")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(m, "ABEL_STATE_FILE", root / "abel_state.json"),
                        patch.object(m, "BOOKS_FILE", root / "books.csv"),
                        patch.object(m, "STATE_FILE", root / "state.json"),
                        patch.object(m, "BOOKS", {}), patch.object(m.time, "sleep")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_failed_send_survives_reload_and_is_sent_only_once_after_success(self):
        legacy = {"abel_new": ["16688"], "abel_sold": ["16688"]}
        with patch.object(m, "notify") as notify:
            m.check_abel(lambda: [card(sold=True)], "test", "test", legacy)
            notify.assert_not_called()
        with patch.object(m, "notify", side_effect=OSError("offline")):
            m.check_abel(lambda: [card()], "test", "test", legacy)
        self.assertEqual(len(m.load_abel_state()["pending"]), 1)
        m.BOOKS = m.load_books()  # как после рестарта процесса
        with patch.object(m, "notify") as notify:
            m.check_abel(lambda: [card()], "test", "test", m.load_state())
            m.check_abel(lambda: [card()], "test", "test", m.load_state())
            self.assertEqual(notify.call_count, 1)
        self.assertEqual(m.load_abel_state()["pending"], [])
        self.assertEqual(m.load_books()["16688"]["price"], "15 000 ₽")

    def test_corrupt_state_fails_instead_of_silent_reseed(self):
        m.ABEL_STATE_FILE.write_text('{"version": 2, "items": {}}')
        with self.assertRaises(ValueError):
            m.load_abel_state()

    def test_empty_initialized_state_cannot_trigger_mass_notifications(self):
        m.ABEL_STATE_FILE.write_text('{"version": 1, "items": {}, "pending": [], "next_event": 1}')
        with self.assertRaises(ValueError):
            m.load_abel_state()

    def test_no_success_ack_keeps_delivery_pending(self):
        with patch.object(m, "tg_call", return_value={"ok": False, "description": "denied"}):
            with self.assertRaises(RuntimeError):
                m.notify("test", "test", dict(card(), tag="Абель – в продаже", images=[]))

    def test_csv_days_are_per_current_cycle(self):
        state = observe([card(sold=True)])
        state = observe([card()], state, day="2026-10-28")
        state = observe([card(sold=True)], state, day="2026-10-30")
        m.sync_abel_books(state)
        self.assertEqual(m.BOOKS["16688"]["days_on_shelf"], "2")


class CatalogueTests(unittest.TestCase):
    def test_footer_stock_message_does_not_mark_available_card_sold(self):
        doc = '''<div class="catalog-card" data-id="16688" data-name="Часть речи" data-price="15000">
        <a href="/product/test/">Книга</a></div>
        <script>BX.message({'NOT_AVAILABLE':'Нет в наличии'});</script>'''
        with patch.object(m, "http_get", return_value=doc):
            cards, _ = m.abel_page(1)
        self.assertFalse(cards[0]["sold"])
        self.assertEqual(cards[0]["price"], "15 000 ₽")

    def test_scan_visits_old_ids_beyond_first_five_pages(self):
        requested = []
        def page(n):
            requested.append(n)
            return [card(str(100 - n))], 8
        with patch.object(m, "abel_page", side_effect=page), patch.object(m.time, "sleep"):
            items = m.source_abel()
        self.assertEqual(set(requested), set(range(1, 9)))
        self.assertIn("92", {it["id"] for it in items})

    def test_broken_later_page_rejects_entire_snapshot(self):
        with patch.object(m, "abel_page", side_effect=lambda n: ([card()] if n == 1 else [], 2)), patch.object(m.time, "sleep"):
            with self.assertRaises(ValueError):
                m.source_abel()

    def test_duplicate_id_with_conflicting_stock_rejects_snapshot(self):
        with patch.object(m, "abel_page", side_effect=lambda n: ([card(sold=n == 2)], 2)), patch.object(m.time, "sleep"):
            with self.assertRaises(ValueError):
                m.source_abel()

    def test_current_gallery_photo_precedes_stale_og_image(self):
        doc = '''<meta property="og:image" content="https://abelbooks.ru/old.webp"/>
        'PICT':{'ID':'41600','SRC':'/upload/current.webp','WIDTH':'1220'}'''
        self.assertEqual(m.abel_cover_images(doc), ["https://abelbooks.ru/upload/current.webp", "https://abelbooks.ru/old.webp"])

    def test_delayed_event_keeps_its_snapshot_photo_not_current_product_photo(self):
        item = dict(card(image="upload/resize_cache/iblock/5dd/hash/306_367_1/copy1.webp"),
                    tag="Абель – продано", cover_page="https://abelbooks.ru/product/test/")
        with patch.object(m, "http_get", return_value=b'\xff\xd8\xffimage') as fetch, patch.object(m, "_send_photo", return_value={"ok": True}):
            m.notify("test", "test", item)
        self.assertEqual(fetch.call_args.args[0], "https://abelbooks.ru/upload/iblock/5dd/hash/copy1.webp")
        self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
