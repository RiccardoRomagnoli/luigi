import unittest

from telegram_client import TelegramClient


class TelegramClientTest(unittest.TestCase):
    def test_filter_messages_allows_chat_and_user(self) -> None:
        client = TelegramClient(bot_token="t", chat_id="123", allowed_user_ids=[1])
        updates = {
            "ok": True,
            "result": [
                {"message": {"text": "hi", "from": {"id": 1}, "chat": {"id": 123}}},
                {"message": {"text": "skip", "from": {"id": 2}, "chat": {"id": 123}}},
            ],
        }
        msgs = client.filter_messages(updates)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "hi")


if __name__ == "__main__":
    unittest.main()

