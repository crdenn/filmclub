"""The endpoint behind "Arrange collections".

Ordering is site furniture rather than authorship, so it is admin-only, and it
applies the whole arrangement or none of it — a client working from a stale
index must not half-apply an order.
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "collection-order-test-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="filmclub-order-bootstrap-"))

from fastapi import HTTPException  # noqa: E402

from app import auth, config, db, main  # noqa: E402
from app import collections as coll  # noqa: E402


class CollectionOrderApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="filmclub-order-")
        self._old_db = config.DB_PATH
        config.DB_PATH = Path(self.tmp.name) / "filmclub.db"
        db.init_db()
        self.conn = db.connect()
        for slug in ("first", "second", "third"):
            coll.create_collection(self.conn, slug, slug.title(),
                                   origin="generated", published=True)

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._old_db
        self.tmp.cleanup()

    def _order(self):
        return [c["slug"] for c in coll.list_collections(self.conn,
                                                         include_unpublished=True)]

    def test_route_requires_admin(self):
        route = next(r for r in main.app.routes
                     if getattr(r, "path", None) == "/api/collections/order")
        calls = {d.call for d in route.dependant.dependencies}
        self.assertIn(auth.require_admin, calls)

    async def test_posting_an_arrangement_applies_it(self):
        result = await main.api_set_collection_order(
            main.CollectionOrderIn(slugs=["third", "first", "second"]), admin={})
        self.assertEqual(result["order"], ["third", "first", "second"])
        self.assertEqual(self._order(), ["third", "first", "second"])

    async def test_a_stale_slug_is_rejected_rather_than_half_applied(self):
        with self.assertRaises(HTTPException) as caught:
            await main.api_set_collection_order(
                main.CollectionOrderIn(slugs=["third", "deleted-since", "first"]),
                admin={})
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("deleted-since", caught.exception.detail)

    async def test_reordering_twice_leaves_no_stale_placement(self):
        await main.api_set_collection_order(
            main.CollectionOrderIn(slugs=["third", "second", "first"]), admin={})
        await main.api_set_collection_order(
            main.CollectionOrderIn(slugs=["second", "third", "first"]), admin={})
        self.assertEqual(self._order(), ["second", "third", "first"])


if __name__ == "__main__":
    unittest.main()
