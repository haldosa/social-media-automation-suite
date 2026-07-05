import unittest
from unittest import mock
from datetime import date

import state


class PublishingPlanTests(unittest.TestCase):
    def setUp(self):
        self.save_patch = mock.patch.object(state, "_save_post_state", lambda data: None)
        self.save_patch.start()

    def tearDown(self):
        self.save_patch.stop()

    def test_daily_plan_starts_with_media_then_text(self):
        data = {}
        profile_id = "profile-a"
        today = date.today().isoformat()

        status = state.get_daily_publishing_status(profile_id, data, today)

        self.assertEqual(status["targets"]["media"], 1)
        self.assertGreaterEqual(status["targets"]["text"], 3)
        self.assertLessEqual(status["targets"]["text"], 5)
        self.assertEqual(
            state.get_next_due_post_kind(
                profile_id,
                data,
                media_available=True,
                today=today,
            ),
            "media",
        )

        state._record_post(profile_id, data, "media")
        self.assertEqual(
            state.get_next_due_post_kind(
                profile_id,
                data,
                media_available=True,
                today=today,
            ),
            "text",
        )

    def test_media_target_does_not_block_text_when_media_unavailable(self):
        data = {}
        profile_id = "profile-b"
        today = date.today().isoformat()

        self.assertEqual(
            state.get_next_due_post_kind(
                profile_id,
                data,
                media_available=False,
                today=today,
            ),
            "text",
        )

    def test_per_kind_quota_blocks_only_that_kind(self):
        data = {}
        profile_id = "profile-c"
        today = date.today().isoformat()
        status = state.get_daily_publishing_status(profile_id, data, today)
        counts = data[profile_id]["daily_post_counts"][today]
        counts["media"] = status["targets"]["media"]

        self.assertFalse(state._can_post_now(profile_id, data, "media"))
        self.assertTrue(state._can_post_now(profile_id, data, "text"))


if __name__ == "__main__":
    unittest.main()
