# Google Trends MCP

Google Trends inside Claude Code. Four tools, no API key, no account.

`pytrends` reads the public Google Trends endpoints, so there is nothing to sign up for and
nothing to store. Google throttles heavy use, and the server retries with a backoff and tells
you when it has been throttled rather than returning a silent empty result.

## Tools

| Tool | What it returns |
|---|---|
| `interest_over_time` | Relative interest for up to five terms across a window |
| `related_queries` | Top and rising queries for a term |
| `interest_by_region` | Where a term indexes highest |
| `trending_now` | What is trending in a country right now |

## Install

```
/plugin install google-trends-mcp@lhitches
```

Then `pip install -r requirements.txt` in the plugin directory, or point the server at a
Python that already has `pytrends` and `mcp` available.

## The one thing to know before you use the numbers

**The 0 to 100 index is relative within a single request.** A term scoring 100 in one query
and 40 in another is not "more popular" in the first: the scale is rebuilt per request against
the highest point in that request. Compare terms inside one call, never across calls.

## Licence

MIT. Built by [Lawrence Hitches](https://www.lawrencehitches.com/#person).
