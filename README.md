# LeadScout

One GET returns fresh public posts where someone is asking for help, looking for a tool, or
offering paid work — scraped from Hacker News (Algolia API), Bluesky (public search API) and
Mastodon (public tag timelines). Built and operated by **genesis-2**, an autonomous AI agent,
human-supervised. Public posts only; every result links to the original.

## Run

    PORT=8462 python3 server.py

Stdlib-only, no dependencies.

## API

- `GET /` — landing page (docs)
- `GET /api/v1/health` — `{"ok":true,...}`
- `GET /api/v1/leads?hours=24&min_score=2&source=hn,bluesky` — JSON `{leads,total,fetched_at}`
  - `hours`: 1–168 (default 24)
  - `min_score`: 1 = help requests, 2 = tool requests, 3 = paid-work signals (default 1)
  - `source`: comma list of hn / bluesky / mastodon
  - `norefresh`: serve from cache only

Scoring is keyword heuristics, **not** LLM-verified intent — results are candidates to judge
yourself. Categories: `paid_work` (paid signal + call-to-action), `tool_request`, `help_request`.
Junk (giveaway/nsfw/etc.) filtered. Rate limit 60 req/min/IP. Cache refreshes every 15 min.
