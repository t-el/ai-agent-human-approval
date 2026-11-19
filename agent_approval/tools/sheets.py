"""Orders lookup in Google Sheets. Mock: samples/orders.csv. Live: Sheets API v4 values.get."""

from __future__ import annotations

import csv
import re
from pathlib import Path

import httpx

from ..retry import check, with_retry

_ORDER = re.compile(r"^ORD-\d{4,6}$", re.I)


class SheetsTool:
    def __init__(self, provider: str = "mock", samples: Path | None = None, sheet_id: str = "",
                 range_: str = "Orders!A:G", api_key: str = "", http: httpx.Client | None = None):
        self.provider, self.sheet_id, self.range, self.key = provider, sheet_id, range_, api_key
        self.http = http or httpx.Client(timeout=15)
        self.samples = samples

    def _rows(self) -> list[dict]:
        if self.provider == "sheets":
            def call():
                r = self.http.get(f"https://sheets.googleapis.com/v4/spreadsheets/{self.sheet_id}/values/{self.range}",
                                  params={"key": self.key})
                return check(r, "sheets values.get").json()["values"]
            values = with_retry(call, step="sheets_lookup")
            header, *rows = values
            return [dict(zip(header, r)) for r in rows]
        with open(self.samples / "orders.csv", newline="") as f:
            return list(csv.DictReader(f))

    def lookup_order(self, order_id: str) -> dict:
        order_id = order_id.strip().upper()
        if not _ORDER.match(order_id):        # validate model-provided input before touching the sheet
            return {"error": f"invalid order id format {order_id!r} (expected ORD-12345)"}
        for row in self._rows():
            if row["order_id"].upper() == order_id:
                return {"found": True, **row}
        return {"found": False, "order_id": order_id}
