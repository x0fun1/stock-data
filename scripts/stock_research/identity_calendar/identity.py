"""Security master and ticker resolution."""

from __future__ import annotations

from datetime import date
from typing import Iterable

from ..common.errors import DataError
from ..common.timeutil import parse_date
from ..contracts import Market, SecurityIdentity, make_security_id


class SecurityMaster:
    def __init__(self, identities: Iterable[SecurityIdentity]) -> None:
        self._by_id: dict[str, SecurityIdentity] = {}
        self._by_ticker: dict[tuple[str, str], list[SecurityIdentity]] = {}
        for identity in identities:
            if identity.security_id in self._by_id:
                raise DataError(f"duplicate security_id {identity.security_id}")
            self._by_id[identity.security_id] = identity
            self._by_ticker.setdefault((identity.market.value, identity.ticker.upper()), []).append(identity)

    def all(self) -> list[SecurityIdentity]:
        return list(self._by_id.values())

    def by_id(self, security_id: str) -> SecurityIdentity | None:
        return self._by_id.get(security_id)

    def resolve(self, market: Market | str, ticker: str, day: date | str | None = None) -> SecurityIdentity:
        market = Market(market)
        day = parse_date(day) if day is not None else None
        candidates = self._by_ticker.get((market.value, ticker.strip().upper()), [])
        if day is not None:
            candidates = [c for c in candidates if c.active_on(day)]
        if not candidates:
            raise DataError(f"security {market.value}:{ticker} is not in the security master for {day}")
        if len(candidates) > 1:
            raise DataError(f"security {market.value}:{ticker} is ambiguous on {day}")
        return candidates[0]

    def add(self, identity: SecurityIdentity) -> None:
        if identity.security_id in self._by_id:
            existing = self._by_id[identity.security_id]
            if existing != identity:
                raise DataError(f"conflicting security_id {identity.security_id}")
            return
        self._by_id[identity.security_id] = identity
        self._by_ticker.setdefault((identity.market.value, identity.ticker.upper()), []).append(identity)

    @classmethod
    def from_rows(cls, rows: Iterable[dict]) -> "SecurityMaster":
        items = []
        for row in rows:
            items.append(
                SecurityIdentity(
                    security_id=str(row.get("security_id") or ""),
                    ticker=str(row["ticker"]),
                    market=str(row["market"]),
                    exchange=str(row.get("exchange", "")),
                    security_type=str(row.get("security_type", "common_stock")),
                    currency=str(row.get("currency", "")),
                    valid_from=parse_date(row["valid_from"]),
                    valid_to=parse_date(row["valid_to"]) if row.get("valid_to") else None,
                    name=str(row.get("name", "")),
                    industry=str(row.get("industry", "")),
                    source_id=str(row.get("source_id", "")),
                )
            )
        return cls(items)


def default_identity(
    market: Market | str,
    ticker: str,
    exchange: str = "",
    currency: str = "",
) -> SecurityIdentity:
    market = Market(market)
    exchange = exchange or ("XNAS" if market is Market.US else "XHKG")
    currency = currency or ("USD" if market is Market.US else "HKD")
    return SecurityIdentity(
        security_id=make_security_id(market, exchange, ticker),
        ticker=ticker.upper(),
        market=market,
        exchange=exchange,
        security_type="common_stock",
        currency=currency,
        valid_from=date(1900, 1, 1),
    )
