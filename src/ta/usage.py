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
#
# Gemini has no entry on purpose. Its price depends on the tier (the free tier
# costs nothing), which this code cannot see; a guessed number would be worse
# than an honest "unknown". Set it in `config.toml` if you pay for it.
DEFAULT_PRICES: dict[str, Price] = {
    "deepseek-flash": Price(input=0.30, output=1.20),
    "deepseek-v4-pro": Price(input=1.32, output=3.96),
}


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
    now: datetime | None = None,
) -> None:
    at = (now or datetime.now()).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO llm_usage (at, provider, model, task, tokens_in, tokens_out, cost_usd)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (at, provider, model, task, usage.tokens_in, usage.tokens_out,
         cost(prices.get(model), usage)),
    )


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
