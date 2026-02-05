import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class TelegramClient:
    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        allowed_user_ids: Optional[List[int]] = None,
        poll_interval_sec: float = 2.0,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.allowed_user_ids = set(allowed_user_ids or [])
        self.poll_interval_sec = poll_interval_sec
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def _post(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/{method}"
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8")
            except Exception:
                raw = ""
            try:
                return json.loads(raw)
            except Exception:
                description = raw.strip() or str(exc)
                return {"ok": False, "description": description}

    def send_message(
        self,
        text: str,
        *,
        return_error: bool = False,
    ) -> bool | tuple[bool, Optional[str]]:
        if not self.bot_token or not self.chat_id:
            if return_error:
                return False, "missing bot_token or chat_id"
            return False
        payload = {"chat_id": self.chat_id, "text": text}
        try:
            res = self._post("sendMessage", payload)
            ok = bool(res.get("ok"))
            error = None
            if not ok:
                error = str(res.get("description") or res)
            if return_error:
                return ok, error
            return ok
        except Exception as exc:
            if return_error:
                return False, str(exc)
            return False

    def poll_updates(self, offset: Optional[int] = None) -> Dict[str, Any]:
        if not self.bot_token:
            return {"ok": False, "result": []}
        payload: Dict[str, Any] = {"timeout": int(self.poll_interval_sec)}
        if offset is not None:
            payload["offset"] = offset
        try:
            res = self._post("getUpdates", payload)
            return res
        except Exception:
            time.sleep(self.poll_interval_sec)
            return {"ok": False, "result": []}

    def filter_messages(self, updates: Dict[str, Any]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if not updates.get("ok"):
            return results
        for item in updates.get("result", []):
            message = item.get("message") or item.get("edited_message")
            if not message:
                continue
            user = message.get("from") or {}
            user_id = user.get("id")
            if self.allowed_user_ids and user_id not in self.allowed_user_ids:
                continue
            chat_id = str(message.get("chat", {}).get("id", ""))
            if chat_id and self.chat_id and chat_id != str(self.chat_id):
                continue
            results.append(message)
        return results
