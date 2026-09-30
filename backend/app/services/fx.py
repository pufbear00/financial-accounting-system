"""汇率服务：Frankfurter 真实日汇率 → CNY（同 bank-flow 口径）。"""
import logging
from datetime import date, timedelta

import httpx

logger = logging.getLogger(__name__)

# 旧域名 api.frankfurter.app 已失效，必须用 .dev/v1

# 仅在 API 不可用时兜底（会记 source 便于事后替换）

# 内存缓存：{ccy: {"YYYY-MM-DD": rate}}
_series_cache: dict[str, dict[str, float]] = {}


async def load_rate_series(ccy: str, start: date, end: date) -> dict[str, float]:
    if ccy == "CNY":
        return {}
    raise RuntimeError("离线版不查询真实汇率")


def rate_for(ccy: str, dt: date) -> float | None:
    """取 dt 当天汇率；周末/节假日回退到之前最近交易日（最多回看 7 天）。"""
    if ccy == "CNY":
        return 1.0
    series = _series_cache.get(ccy, {})
    for i in range(8):
        rate = series.get((dt - timedelta(days=i)).isoformat())
        if rate:
            return rate
    return None


async def fill_airwallex_cny(rows) -> int:
    updated = 0
    for row in rows:
        if row.currency == "CNY" and row.amount_cny is None:
            row.exchange_rate = 1.0
            row.amount_cny = row.amount
            updated += 1
    return updated
