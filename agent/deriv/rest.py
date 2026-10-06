"""Deriv REST calls: list accounts and fetch a one-time-password WebSocket URL."""

from __future__ import annotations

from typing import Any

import httpx

from agent.config import Settings
from agent.deriv.guard import RealAccountRefused, assert_demo_account, assert_demo_ws_url


class DerivRest:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._s = settings
        self._client = client
        self._account_id: str | None = settings.deriv_account_id or None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._s.deriv_pat.get_secret_value()}",
            "Deriv-App-ID": self._s.deriv_app_id,
            "Content-Type": "application/json",
        }

    async def list_accounts(self) -> list[dict[str, Any]]:
        r = await self._client.get(
            f"{self._s.deriv_rest_url}/trading/v1/options/accounts", headers=self._headers()
        )
        r.raise_for_status()
        body = r.json()
        items = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(items, dict):
            items = items.get("accounts", [items])
        return [a for a in items if isinstance(a, dict)]

    async def demo_account_id(self) -> str:
        if self._account_id:
            return self._account_id
        demo = []
        for acc in await self.list_accounts():
            try:
                assert_demo_account(acc)
            except RealAccountRefused:
                continue
            acc_id = acc.get("account_id") or acc.get("id") or acc.get("loginid")
            if acc_id:
                demo.append(str(acc_id))
        if len(demo) != 1:
            raise RealAccountRefused(
                f"expected exactly one demo account, found {len(demo)}; set DERIV_ACCOUNT_ID"
            )
        self._account_id = demo[0]
        return self._account_id

    async def demo_ws_url(self) -> str:
        """Fresh OTP WebSocket URL for the demo account. Called on every (re)connect."""
        account_id = await self.demo_account_id()
        r = await self._client.post(
            f"{self._s.deriv_rest_url}/trading/v1/options/accounts/{account_id}/otp",
            headers=self._headers(),
        )
        r.raise_for_status()
        url = str(r.json()["data"]["url"])
        assert_demo_ws_url(url, self._s.deriv_ws_allowed_hosts)
        return url
