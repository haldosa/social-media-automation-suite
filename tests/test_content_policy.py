import unittest

from content_policy import (
    ContentPolicyError,
    prepare_caption_for_publishing,
    validate_caption,
    validate_reply,
)


class CaptionPolicyTests(unittest.TestCase):
    def test_preparation_is_deterministic_and_format_only(self):
        source = "  Product\u200b   updates\r\nare available now.  "
        expected = "Product updates\nare available now."
        self.assertEqual(prepare_caption_for_publishing(source), expected)
        self.assertEqual(prepare_caption_for_publishing(source), expected)

    def test_professional_caption_is_valid(self):
        self.assertTrue(
            validate_caption("A clear process helps teams publish consistently.")
        )

    def test_empty_and_emoji_only_captions_are_rejected(self):
        self.assertFalse(validate_caption("   "))
        self.assertFalse(validate_caption("🚀✨"))

    def test_caption_limits_are_enforced(self):
        voice = {"max_emojis": 1, "max_hashtags": 2}
        self.assertFalse(validate_caption("New release 🚀 ✨", voice))
        self.assertFalse(validate_caption("Update #one #two #three", voice))
        self.assertTrue(validate_caption("Engineering update 👩‍💻", voice))

    def test_spam_repetition_and_concatenation_are_rejected(self):
        self.assertFalse(validate_caption("Follow for follow and guaranteed results."))
        self.assertFalse(validate_caption("Update update update for the team."))
        self.assertFalse(validate_caption("First approved caption.Second unrelated caption."))

    def test_banned_terms_and_abbreviation_allowlist_are_enforced(self):
        self.assertFalse(validate_caption("This update is useful tbh."))
        self.assertTrue(
            validate_caption(
                "This update is useful tbh.",
                {"allowed_abbreviations": ["tbh"]},
            )
        )
        self.assertFalse(
            validate_caption(
                "Our miracle offer is ready.",
                {"banned_terms": ["miracle offer"]},
            )
        )

    def test_preparation_raises_for_invalid_caption(self):
        with self.assertRaises(ContentPolicyError):
            prepare_caption_for_publishing("Click the link!!!!")


class ReplyPolicyTests(unittest.TestCase):
    def test_business_safe_reply_is_valid(self):
        self.assertTrue(validate_reply("Thank you for sharing this perspective."))

    def test_empty_short_spammy_and_linked_replies_are_rejected(self):
        self.assertFalse(validate_reply(""))
        self.assertFalse(validate_reply("Great post"))
        self.assertFalse(validate_reply("Please DM me for details."))
        self.assertFalse(validate_reply("Read the details at https://example.com now."))

    def test_reply_uses_brand_voice_limits(self):
        voice = {"max_emojis": 0, "max_hashtags": 0}
        self.assertFalse(validate_reply("Thank you for the thoughtful update. ✅", voice))
        self.assertFalse(validate_reply("Thank you for the thoughtful update. #news", voice))


if __name__ == "__main__":
    unittest.main()
