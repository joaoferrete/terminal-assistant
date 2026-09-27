"""What the model calls cost: one row per answered call, and the sum per provider.

Prices are per million tokens, in USD, and belong to a **model**. A model with
no known price is recorded with `cost_usd` NULL — unknown, not free — and a sum
over a period says how many calls it could not price, instead of quietly
reporting a smaller number than the real one.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .providers import Usage

log = logging.getLogger("ta")


@dataclass(frozen=True)
class Price:
    input: float    # USD per 1M input tokens
    output: float   # USD per 1M output tokens


# DeepSeek's published price on 2026-09-25, at **peak** rate and without the
# cache-hit discount. So it is an upper bound: off-peak costs half, and a cached
# prompt far less. An upper bound is the useful direction to be wrong in for a
# number whose job is to warn about spend.
DEFAULT_PRICES: dict[str, Price] = {
    "deepseek-flash": Price(input=0.30, output=1.20),
    "deepseek-v4-pro": Price(input=1.32, output=3.96),
    # Gemini's paid tier on 2026-09-25, for `gemini-flash-latest` (then 3.8 Flash).
    # It is here after all, despite the free tier: web search runs on Gemini, and
    # an unpriced search would escape the cost ceiling (D30). An upper bound for
    # a free-tier key, and it DOUBLES on 2027-01-01 — update it then.
    "gemini-flash-latest": Price(input=0.75, output=3.75),
}

# Grounding with Google Search, per request, past the 5,000 free a month. Counted
# on every search, free allowance ignored: an upper bound, as everywhere here.
SEARCH_PER_CALL = 0.014


def cost(price: Price | None, usage: Usage) -> float | None:
    if price is None:
        return None
    return (usage.tokens_in * price.input + usage.tokens_out * price.output) / 1_000_000


def record(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    task: str,
    usage: Usage,
    prices: dict[str, Price],
    member_id: int | None = None,
    now: datetime | None = None,
) -> None:
    at = (now or datetime.now()).isoformat(timespec="seconds")
    spent = cost(prices.get(model), usage)
    if task == "web_search" and spent is not None:
        spent += SEARCH_PER_CALL
    conn.execute(
        "INSERT INTO llm_usage (at, provider, model, task, tokens_in, tokens_out, cost_usd,"
        " member_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (at, provider, model, task, usage.tokens_in, usage.tokens_out, spent, member_id),
    )


def _estimated_sum(prices: dict[str, Price]) -> str:
    """SQL for the spend of a set of rows, with unpriced calls counted at the most
    expensive known price — so they cannot slip under a ceiling (D30)."""
    worst_in = max((p.input for p in prices.values()), default=0)
    worst_out = max((p.output for p in prices.values()), default=0)
    return (f"COALESCE(SUM(COALESCE(cost_usd, (tokens_in * {worst_in}"
            f" + tokens_out * {worst_out}) / 1000000.0)), 0)")


def spent_by_member(conn: sqlite3.Connection, member_id: int, since: datetime,
                    prices: dict[str, Price]) -> float:
    return conn.execute(
        f"SELECT {_estimated_sum(prices)} FROM llm_usage WHERE member_id = ? AND at >= ?",
        (member_id, since.isoformat(timespec="seconds")),
    ).fetchone()[0]


def spent_total(conn: sqlite3.Connection, since: datetime, prices: dict[str, Price]) -> float:
    return conn.execute(
        f"SELECT {_estimated_sum(prices)} FROM llm_usage WHERE at >= ?",
        (since.isoformat(timespec="seconds"),),
    ).fetchone()[0]


@dataclass(frozen=True)
class ProviderSpend:
    provider: str
    calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float     # over the calls that had a price
    unpriced_calls: int


def spend_since(conn: sqlite3.Connection, since: datetime) -> list[ProviderSpend]:
    rows = conn.execute(
        """
        SELECT provider,
               COUNT(*),
               SUM(tokens_in),
               SUM(tokens_out),
               COALESCE(SUM(cost_usd), 0),
               SUM(cost_usd IS NULL)
          FROM llm_usage
         WHERE at >= ?
         GROUP BY provider
         ORDER BY provider
        """,
        (since.isoformat(timespec="seconds"),),
    ).fetchall()
    return [ProviderSpend(*tuple(r)) for r in rows]
