"""Member colour allocation and legacy-collision repair tests."""
import sqlite3
import unittest

from app import colors


class MemberColorTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE members (id INTEGER PRIMARY KEY, plex_id TEXT, color TEXT)"
        )

    def tearDown(self):
        self.conn.close()

    def test_allocator_skips_colors_already_in_use(self):
        preferred = colors.color_for("plex:alice")
        allocated = colors.color_for("plex:alice", [preferred])

        self.assertNotEqual(allocated.lower(), preferred.lower())
        self.assertIn(allocated, colors.PALETTE)

    def test_allocator_remains_unique_after_palette_is_full(self):
        allocated = colors.color_for("plex:extra", colors.PALETTE)

        self.assertNotIn(allocated.lower(), {color.lower() for color in colors.PALETTE})
        self.assertRegex(allocated, r"^#[0-9a-f]{6}$")

    def test_reconcile_changes_only_later_duplicate_and_blank_rows(self):
        duplicate = colors.PALETTE[3]
        self.conn.executemany(
            "INSERT INTO members (id, plex_id, color) VALUES (?, ?, ?)",
            [
                (1, "plex:one", duplicate),
                (2, "plex:two", colors.PALETTE[5]),
                (3, "plex:three", duplicate),
                (4, "plex:four", ""),
            ],
        )

        changed = colors.reconcile_member_colors(self.conn)
        rows = self.conn.execute(
            "SELECT id, color FROM members ORDER BY id"
        ).fetchall()

        self.assertEqual(changed, 2)
        self.assertEqual(rows[0]["color"], duplicate)
        self.assertEqual(rows[1]["color"], colors.PALETTE[5])
        self.assertEqual(len({row["color"].lower() for row in rows}), 4)

    def test_available_color_considers_uncommitted_members(self):
        first = colors.color_for("plex:first")
        self.conn.execute(
            "INSERT INTO members (plex_id, color) VALUES (?, ?)",
            ("plex:first", first),
        )

        allocated = colors.available_color(self.conn, "plex:second")

        self.assertNotEqual(allocated.lower(), first.lower())


if __name__ == "__main__":
    unittest.main()
