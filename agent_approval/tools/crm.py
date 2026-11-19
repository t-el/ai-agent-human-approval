"""CRM lookup. Mock: samples/crm.json. Live: HubSpot CRM v3 contacts search (POST /crm/v3/objects/contacts/search)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from ..retry import check, with_retry


class CrmTool:
    def __init__(self, provider: str = "mock", samples: Path | None = None, hubspot_token: str = "",
                 http: httpx.Client | None = None, fail: bool = False):
        self.provider, self.token, self.fail = provider, hubspot_token, fail
        self.http = http or httpx.Client(timeout=15)
        self.data = json.loads((samples / "crm.json").read_text()) if samples else {}

    def lookup(self, email: str) -> dict:
        email = email.strip().lower()
        if self.fail:
            raise ConnectionError("CRM API timed out")
        if self.provider == "hubspot":
            return self._hubspot(email)
        c = self.data.get(email)
        return {"found": True, "email": email, **c} if c else {"found": False, "email": email}

    def _hubspot(self, email: str) -> dict:
        body = {"filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
                "properties": ["firstname", "lastname", "company", "hs_lead_status", "lifecyclestage",
                               "hubspot_owner_id", "num_associated_deals"], "limit": 1}

        def call():
            r = self.http.post("https://api.hubapi.com/crm/v3/objects/contacts/search", json=body,
                               headers={"Authorization": f"Bearer {self.token}"})
            return check(r, "hubspot search").json()

        res = with_retry(call, step="crm_lookup")
        if not res.get("results"):
            return {"found": False, "email": email}
        p = res["results"][0]["properties"]
        return {"found": True, "email": email, "name": f"{p.get('firstname', '')} {p.get('lastname', '')}".strip(),
                "company": p.get("company"), "lifecycle": p.get("lifecyclestage"), "owner_id": p.get("hubspot_owner_id")}
