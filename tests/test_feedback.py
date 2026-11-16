import unittest
from unittest.mock import patch

from github_bridge.feedback import Drafts, group_context, redact


class FeedbackTests(unittest.TestCase):
    def test_draft_is_sender_bound_single_use_and_expires(self):
        drafts = Drafts()
        draft = drafts.create("qq:FriendMessage:1", "owner/repo", "title", "body")
        with self.assertRaises(ValueError):
            drafts.pop(draft.code, "qq:FriendMessage:2")
        self.assertEqual(drafts.pop(draft.code, draft.sender), draft)
        with self.assertRaises(ValueError):
            drafts.pop(draft.code, draft.sender)
        draft = drafts.create("sender", "owner/repo", "title", "body")
        with patch("github_bridge.feedback.time.time", return_value=draft.expires + 1):
            with self.assertRaises(ValueError):
                drafts.pop(draft.code, "sender")

    def test_context_is_text_only_and_omits_sender_ids_and_media(self):
        result = group_context(
            {
                "messages": [
                    {
                        "sender": {"user_id": "private-identity"},
                        "message": [
                            {
                                "type": "text",
                                "data": {"text": "导入失败 token=private-token"},
                            },
                            {"type": "image", "data": {"url": "https://private-image"}},
                        ],
                    }
                ]
            }
        )
        self.assertIn("导入失败", result)
        self.assertNotIn("private-identity", result)
        self.assertNotIn("private-image", result)
        self.assertNotIn("private-token", result)

    def test_redacts_common_secrets_and_preserves_useful_text(self):
        text = redact(
            "重现步骤：导入\nAuthorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz\n密码=abcdef"
        )
        self.assertIn("导入", text)
        self.assertNotIn("ghp_", text)
        self.assertNotIn("abcdef", text)
