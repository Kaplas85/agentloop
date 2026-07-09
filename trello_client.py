"""Thin wrapper over the Trello REST API used by the agent loop."""

from __future__ import annotations

import requests

API_BASE = "https://api.trello.com/1"


class TrelloError(RuntimeError):
    pass


class TrelloClient:
    def __init__(self, api_key: str, token: str, board_id: str):
        self.api_key = api_key
        self.token = token
        self.board_id = board_id
        self._session = requests.Session()

    def _auth_params(self, **extra):
        return {"key": self.api_key, "token": self.token, **extra}

    def _get(self, path: str, **params) -> object:
        resp = self._session.get(f"{API_BASE}{path}", params=self._auth_params(**params))
        if not resp.ok:
            raise TrelloError(f"GET {path} failed: {resp.status_code} {resp.text}")
        return resp.json()

    def _post(self, path: str, **params) -> object:
        resp = self._session.post(f"{API_BASE}{path}", params=self._auth_params(**params))
        if not resp.ok:
            raise TrelloError(f"POST {path} failed: {resp.status_code} {resp.text}")
        return resp.json()

    def _put(self, path: str, **params) -> object:
        resp = self._session.put(f"{API_BASE}{path}", params=self._auth_params(**params))
        if not resp.ok:
            raise TrelloError(f"PUT {path} failed: {resp.status_code} {resp.text}")
        return resp.json()

    def lists(self) -> list[dict]:
        return self._get(f"/boards/{self.board_id}/lists")

    def list_id_by_name(self, name: str) -> str | None:
        for lst in self.lists():
            if lst["name"].strip().lower() == name.strip().lower():
                return lst["id"]
        return None

    def cards_in_list(self, list_id: str) -> list[dict]:
        return self._get(f"/lists/{list_id}/cards")

    def move_card(self, card_id: str, list_id: str) -> None:
        self._put(f"/cards/{card_id}", idList=list_id)

    def add_comment(self, card_id: str, text: str) -> None:
        self._post(f"/cards/{card_id}/actions/comments", text=text)

    def comments(self, card_id: str) -> list[dict]:
        actions = self._get(f"/cards/{card_id}/actions", filter="commentCard")
        return [a["data"]["text"] for a in actions]
