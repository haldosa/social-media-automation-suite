import os
import tempfile
import unittest

from profile_content import (
    ContentConfigurationError,
    get_profile_content,
    media_history_key,
    resolve_approved_media,
)


class ProfileContentTests(unittest.TestCase):
    def test_profile_overrides_defaults_exactly(self):
        data = {
            "defaults": {
                "approved_replies": ["Default reply."],
                "approved_captions": ["Default caption."],
                "approved_media": ["default.jpg"],
                "search_topics": ["default search"],
            },
            "profiles": {
                "profile-a": {
                    "approved_replies": ["Profile reply."],
                    "approved_captions": ["Profile caption."],
                    "approved_media": ["profile-a/post.jpg"],
                }
            },
        }

        content = get_profile_content(data, "profile-a")

        self.assertEqual(content["source"], "profile")
        self.assertEqual(content["approved_replies"], ["Profile reply."])
        self.assertEqual(content["approved_captions"], ["Profile caption."])
        self.assertEqual(content["approved_media"], ["profile-a/post.jpg"])
        self.assertEqual(content["search_topics"], ["default search"])

    def test_empty_profile_list_is_intentional_override(self):
        data = {
            "defaults": {"approved_replies": ["Default reply."]},
            "profiles": {"profile-a": {"approved_replies": []}},
        }

        self.assertEqual(get_profile_content(data, "profile-a")["approved_replies"], [])

    def test_top_level_legacy_keys_still_migrate_to_defaults(self):
        data = {
            "comments": ["Legacy reply."],
            "post_captions": ["Legacy caption."],
            "search_topics": ["legacy search"],
        }

        content = get_profile_content(data, "unknown")

        self.assertEqual(content["approved_replies"], ["Legacy reply."])
        self.assertEqual(content["approved_captions"], ["Legacy caption."])
        self.assertEqual(content["search_topics"], ["legacy search"])

    def test_invalid_profile_content_fails_closed(self):
        with self.assertRaises(ContentConfigurationError):
            get_profile_content({"profiles": {"profile-a": {"approved_replies": "nope"}}}, "profile-a")


class ProfileMediaTests(unittest.TestCase):
    def test_resolves_only_existing_safe_relative_media(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "profile-a"))
            approved_file = os.path.join(root, "profile-a", "post.jpg")
            with open(approved_file, "wb") as f:
                f.write(b"image")
            with open(os.path.join(root, "profile-a", "notes.txt"), "wb") as f:
                f.write(b"text")

            approved, errors = resolve_approved_media(
                root,
                ["profile-a/post.jpg", "profile-a/missing.png", "../outside.jpg", "profile-a/notes.txt"],
                [".jpg", ".png"],
            )

            self.assertEqual(approved, [approved_file])
            self.assertEqual(len(errors), 3)
            self.assertEqual(media_history_key(root, approved_file), "profile-a/post.jpg")


if __name__ == "__main__":
    unittest.main()
