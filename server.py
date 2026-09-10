"""Google Trends MCP server.

Exposes Google Trends data (via the unofficial `pytrends` library) to Claude as
MCP tools:

  - interest_over_time   : search-interest time series for one or more terms
  - related_queries      : top + rising related queries for a term
  - interest_by_region   : geographic breakdown of interest for a term
  - trending_now         : currently trending searches for a country

pytrends talks to Google's internal Trends endpoints. There is no official public
API, so Google rate-limits aggressively (HTTP 429). Every tool retries with backoff
and returns a plain-language error rather than a stack trace when Google throttles.

Run:  python server.py         (stdio transport, for Claude Desktop / Claude Code)
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Any

import pandas as pd
import requests
from pytrends.request import TrendReq
from requests.exceptions import RequestException

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("google-trends")

# --- Trends session -------------------------------------------------------

# hl = host language, tz = timezone offset in minutes (Australia AEST ~ -600,
# but pytrends uses US-minutes convention; 660 = UTC+11). Kept simple here.
_HL = "en-AU"
_TZ = 660


def _client() -> TrendReq:
    """Fresh pytrends session. A new session per call avoids stale-cookie 429s.

    Note: we deliberately do NOT pass pytrends' own ``retries``/``backoff_factor``.
    Those make pytrends build a urllib3 ``Retry`` with the ``method_whitelist`` arg,
    which urllib3 v2 removed. Our ``_with_retry`` wrapper handles retries instead.
    """
    return TrendReq(hl=_HL, tz=_TZ, timeout=(10, 25))


def _with_retry(fn, *, attempts: int = 3, base_delay: float = 2.0):
    """Call `fn`, retrying on transient Google throttling / network errors."""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as err:  # noqa: BLE001 - surfaced to the caller below
            last_err = err
            msg = str(err).lower()
            transient = (
                "429" in msg
                or "too many requests" in msg
                or "timed out" in msg
                or isinstance(err, RequestException)
            )
            if not transient or i == attempts - 1:
                break
            time.sleep(base_delay * (2 ** i))
    raise last_err  # type: ignore[misc]


def _throttle_hint(err: Exception) -> str:
    return (
        f"Google Trends request failed: {err}. This is usually rate-limiting "
        "(HTTP 429) because pytrends uses Google's unofficial endpoints. Wait a "
        "minute and try again, or narrow the request (fewer keywords / shorter "
        "timeframe)."
    )


# --- Tools ----------------------------------------------------------------

@mcp.tool()
def interest_over_time(
    keywords: list[str],
    timeframe: str = "today 12-m",
    geo: str = "",
) -> dict[str, Any]:
    """Search-interest time series for one or more terms (the core Trends chart).

    Values are Google's 0-100 relative index, normalised across the terms in the
    single request so they can be compared to each other.

    Args:
        keywords: 1-5 search terms, e.g. ["seo agency", "digital marketing"].
        timeframe: Google Trends range string. Examples:
            "now 1-H", "now 7-d", "today 1-m", "today 3-m", "today 12-m",
            "today 5-y", "all", or an explicit "2024-01-01 2024-12-31".
        geo: Two-letter country code (e.g. "AU", "US", "GB") or "" for worldwide.
            Sub-regions also work, e.g. "AU-NSW".

    Returns:
        dict with the resolved query and a list of {date, <term>:value, ...} rows.
    """
    kw = [k.strip() for k in keywords if k and k.strip()][:5]
    if not kw:
        return {"error": "Provide at least one keyword."}

    def run():
        py = _client()
        py.build_payload(kw, cat=0, timeframe=timeframe, geo=geo, gprop="")
        return py.interest_over_time()

    try:
        df: pd.DataFrame = _with_retry(run)
    except Exception as err:  # noqa: BLE001
        return {"error": _throttle_hint(err)}

    if df is None or df.empty:
        return {
            "query": {"keywords": kw, "timeframe": timeframe, "geo": geo or "worldwide"},
            "rows": [],
            "note": "No data returned. The term may be too low-volume, or the geo/timeframe too narrow.",
        }

    if "isPartial" in df.columns:
        df = df.drop(columns=["isPartial"])

    rows = [
        {"date": idx.strftime("%Y-%m-%d"), **{c: int(r[c]) for c in df.columns}}
        for idx, r in df.iterrows()
    ]
    # Peak per term is handy for a quick read without scanning every row.
    peaks = {c: {"value": int(df[c].max()), "date": df[c].idxmax().strftime("%Y-%m-%d")} for c in df.columns}
    return {
        "query": {"keywords": kw, "timeframe": timeframe, "geo": geo or "worldwide"},
        "peaks": peaks,
        "rows": rows,
    }


@mcp.tool()
def related_queries(
    keyword: str,
    timeframe: str = "today 12-m",
    geo: str = "",
    limit: int = 25,
) -> dict[str, Any]:
    """Top and rising related queries for a single term — the content-idea goldmine.

    "top" = most popular related searches (relative 0-100). "rising" = fastest-growing
    (value is percent growth; "Breakout" means >5000% growth).

    Args:
        keyword: A single search term.
        timeframe: Google Trends range string (see interest_over_time).
        geo: Two-letter country code or "" for worldwide.
        limit: Max rows to return per list (default 25).
    """
    kw = keyword.strip()
    if not kw:
        return {"error": "Provide a keyword."}

    def run():
        py = _client()
        py.build_payload([kw], cat=0, timeframe=timeframe, geo=geo, gprop="")
        return py.related_queries()

    try:
        data = _with_retry(run)
    except Exception as err:  # noqa: BLE001
        return {"error": _throttle_hint(err)}

    block = (data or {}).get(kw) or {}

    def _fmt(df: pd.DataFrame | None) -> list[dict[str, Any]]:
        if df is None or df.empty:
            return []
        out = []
        for _, r in df.head(limit).iterrows():
            val = r.get("value")
            out.append({"query": r["query"], "value": None if pd.isna(val) else (int(val) if float(val).is_integer() else val)})
        return out

    top = _fmt(block.get("top"))
    rising = _fmt(block.get("rising"))
    return {
        "query": {"keyword": kw, "timeframe": timeframe, "geo": geo or "worldwide"},
        "top": top,
        "rising": rising,
        "note": "Empty lists usually mean the term is too low-volume for related-query data." if not top and not rising else None,
    }


@mcp.tool()
def interest_by_region(
    keyword: str,
    resolution: str = "COUNTRY",
    timeframe: str = "today 12-m",
    geo: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    """Geographic breakdown of interest for a term (which places search it most).

    Args:
        keyword: A single search term.
        resolution: "COUNTRY" (worldwide), "REGION" (states/territories within a
            country — requires geo set, e.g. geo="AU"), "CITY", or "DMA" (US metro).
        timeframe: Google Trends range string.
        geo: Two-letter country code, or "" for worldwide. Required for REGION/CITY.
        limit: Max regions to return, ranked by interest (default 30).
    """
    kw = keyword.strip()
    if not kw:
        return {"error": "Provide a keyword."}
    res = resolution.strip().upper()
    if res not in {"COUNTRY", "REGION", "CITY", "DMA"}:
        return {"error": 'resolution must be one of "COUNTRY", "REGION", "CITY", "DMA".'}
    if res in {"REGION", "CITY"} and not geo:
        return {"error": f'resolution "{res}" needs a country in geo, e.g. geo="AU".'}

    def run():
        py = _client()
        py.build_payload([kw], cat=0, timeframe=timeframe, geo=geo, gprop="")
        return py.interest_by_region(resolution=res, inc_low_vol=True, inc_geo_code=False)

    try:
        df: pd.DataFrame = _with_retry(run)
    except Exception as err:  # noqa: BLE001
        return {"error": _throttle_hint(err)}

    if df is None or df.empty:
        return {"query": {"keyword": kw, "resolution": res, "geo": geo or "worldwide"}, "regions": []}

    col = kw if kw in df.columns else df.columns[0]
    df = df[df[col] > 0].sort_values(col, ascending=False).head(limit)
    regions = [{"region": idx, "interest": int(r[col])} for idx, r in df.iterrows()]
    return {
        "query": {"keyword": kw, "resolution": res, "geo": geo or "worldwide"},
        "regions": regions,
    }


@mcp.tool()
def trending_now(geo: str = "AU", limit: int = 20) -> dict[str, Any]:
    """Currently trending searches for a country — for newsjacking / reactive content.

    Reads Google's live Trending RSS feed (the ``trends.google.com/trending/rss``
    endpoint). Each item includes the search term, approximate traffic, and the
    news headlines driving it.

    Note: pytrends' own ``trending_searches`` was retired by Google (returns 404),
    so this tool talks to the RSS feed directly instead.

    Args:
        geo: Two-letter country code, e.g. "AU", "US", "GB", "NZ", "IN".
        limit: Max trending items to return (default 20).
    """
    cc = geo.strip().upper()
    if len(cc) != 2:
        return {"error": f'geo must be a two-letter country code (e.g. "AU", "US"); got "{geo}".'}

    url = f"https://trends.google.com/trending/rss?geo={cc}"

    def run():
        resp = requests.get(url, timeout=(10, 25), headers={"User-Agent": "Mozilla/5.0 (google-trends-mcp)"})
        resp.raise_for_status()
        return resp.content

    try:
        raw = _with_retry(run)
    except Exception as err:  # noqa: BLE001
        return {"error": _throttle_hint(err) + f' Check the geo code "{cc}".'}

    ns = {"ht": "https://trends.google.com/trending/rss"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as err:
        return {"error": f"Could not parse the trending feed for {cc}: {err}"}

    items: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        traffic = (item.findtext("ht:approx_traffic", default="", namespaces=ns) or "").strip()
        news = []
        for n in item.findall("ht:news_item", ns):
            headline = (n.findtext("ht:news_item_title", default="", namespaces=ns) or "").strip()
            src = (n.findtext("ht:news_item_source", default="", namespaces=ns) or "").strip()
            if headline:
                news.append({"headline": headline, "source": src})
        items.append({
            "term": title,
            "approx_traffic": traffic or None,
            "top_headline": news[0] if news else None,
        })
        if len(items) >= limit:
            break

    return {
        "geo": cc,
        "trending": items,
        "note": "Empty means no live trending feed for this country code." if not items else None,
    }


if __name__ == "__main__":
    mcp.run()
