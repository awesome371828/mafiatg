import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

import main


def signed_init_data(user: dict, auth_date: int | None = None) -> str:
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAE-test-query",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", main.BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class TelegramInitDataTests(unittest.TestCase):
    def test_valid_signed_data_returns_user(self):
        user = {"id": 6652898792, "first_name": "Owner", "username": "owner"}
        result = main.verify_telegram_init_data(signed_init_data(user))
        self.assertEqual(result["id"], user["id"])
        self.assertEqual(result["username"], "owner")

    def test_tampered_user_is_rejected(self):
        raw = signed_init_data({"id": 123, "first_name": "Player"})
        self.assertIsNone(main.verify_telegram_init_data(raw.replace("%22id%22%3A123", "%22id%22%3A999")))

    def test_expired_data_is_rejected(self):
        old = int(time.time()) - 90000
        self.assertIsNone(main.verify_telegram_init_data(signed_init_data({"id": 123}, old)))

    def test_missing_hash_is_rejected(self):
        self.assertIsNone(main.verify_telegram_init_data("user=%7B%22id%22%3A123%7D&auth_date=1"))


if __name__ == "__main__":
    unittest.main()
