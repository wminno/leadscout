#!/usr/bin/env python3
"""LeadScout API — finds fresh asks across HN, Bluesky, Mastodon.
Free, no signup. Built and operated by genesis-2 (autonomous AI agent).
Public posts only; every lead links back to its source."""
import json, os, re, time, threading, urllib.request, urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('PORT', '8462'))
REFRESH_S = int(os.environ.get('REFRESH_S', '900'))      # 15 min
MAX_AGE_H = int(os.environ.get('MAX_AGE_H', '48'))
RATE = int(os.environ.get('RATE_PER_MIN', '60'))

PAID = re.compile(r'(will pay|pay you|\bpaid\b|paid gig|for hire|hiring|freelance|bounty|\$\s?\d|budget|looking to hire|who can build|need someone to)', re.I)
ASK = re.compile(r'(dm me|hit me up|reach out|email (me|us)|apply|hiring|for hire|looking for|need someone|anyone know|recommend|available for|rates|my portfolio|open to work|gig|commission|open role|job opening|we are hiring|send (me|us|your))', re.I)
TOOLREQ = re.compile(r'(anyone know (a|of) (a )?tool|is there a tool|looking for a tool|recommend a tool|alternative to|tool for|looking for (?:an? )?(?:app|script|service))', re.I)
HELP = re.compile(r'(need help|help me|can someone|how do i|advice on|stuck on)', re.I)
JUNK = re.compile(r'(nude|crypto giveaway|giveaway|airdrop|casino|nsfw|18\+|porn|escort)', re.I)

_lock = threading.Lock()
CACHE = {'leads': {}, 'fetched_at': 0}
RATEBOX = {}  # ip -> [count, window_start]

def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'leadscout-api/1.0 (by genesis-2)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception:
        return None

def strip_html(s):
    return re.sub(r'\s+', ' ', re.sub('<[^>]+>', ' ', s or '')).strip()

def collect():
    """Fetch all sources; return list of lead dicts."""
    since = int(time.time()) - MAX_AGE_H * 3600
    leads = []
    hn_q = ['"will pay"', '"looking for someone"', 'bounty', '"anyone know a tool"',
            '"is there a tool"', '"need help"', '"looking for" freelance']
    for q in hn_q:
        d = get_json(f'https://hn.algolia.com/api/v1/search_by_date?query={urllib.parse.quote(q)}'
                     f'&tags=comment&numericFilters=created_at_i>{since}&hitsPerPage=15')
        for h in (d or {}).get('hits', []):
            txt = strip_html(h.get('comment_text') or '')
            if txt.startswith('&gt;') or txt.startswith('>'): continue  # quoted text
            if len(txt) > 40:
                leads.append({'source': 'hn', 'id': 'hn_' + h['objectID'], 'text': txt[:500],
                              'url': f"https://news.ycombinator.com/item?id={h['objectID']}",
                              'context': h.get('story_title'), 'author': h.get('author'),
                              'ts': h.get('created_at_i')})
    bsky_q = ['"need someone to" script', '"anyone know" tool automate', '"looking for" freelance developer',
              '"will pay" someone', 'spreadsheet help paid', '"need a" bot']
    for q in bsky_q:
        d = get_json(f'https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={urllib.parse.quote(q)}&limit=20&sort=latest')
        for p in (d or {}).get('posts', []):
            txt = p.get('record', {}).get('text', '')
            if len(txt) > 40:
                handle = p['author']['handle']; rkey = p['uri'].split('/')[-1]
                try:
                    ts = int(datetime.strptime(p['record']['createdAt'][:19], '%Y-%m-%dT%H:%M:%S').timestamp())
                except Exception:
                    ts = 0
                leads.append({'source': 'bluesky', 'id': 'bsky_' + p['uri'], 'text': txt[:500],
                              'url': f'https://bsky.app/profile/{handle}/post/{rkey}',
                              'context': handle, 'author': handle, 'ts': ts})
    for tag in ['forhire', 'freelance', 'bounty', 'jobs', 'codinghelp']:
        d = get_json(f'https://mastodon.social/api/v1/timelines/tag/{tag}?limit=25')
        for p in d or []:
            txt = strip_html(p.get('content'))
            if len(txt) > 40:
                leads.append({'source': 'mastodon', 'id': 'masto_' + p['uri'], 'text': txt[:500],
                              'url': p.get('url'), 'context': p['account']['acct'],
                              'author': p['account']['acct'], 'ts': int(time.mktime(time.strptime(p['created_at'][:19], '%Y-%m-%dT%H:%M:%S')))})
    return leads

NEG = re.compile(r'(will|would|should|must|could|might|may)\s+(also\s+|never\s+|not\s+|have to\s+|be\s+)?pay\s+(the\s+)?(extra|more|less|taxes|for (that|this|it|their|the)|a (premium|lot|fraction|cent|dollar|pound|euro))|pay(ing|s)?\s+(taxes|movie|him|her|them|someone else|to (see|watch|hear))|paid\s+(movie|him|her|them|someone|to )|bug bounty|bounty (money|was|hunter|program)|who will pay|who pays|will pay for|pay the extra|paying extra', re.I)
FIRST = re.compile(r"(i('|a)m |i am |we are |we're |i need |we need |i'm looking|we're looking|we are looking|dm me|hit me up|reach out|email me|my (portfolio|rates|resume)|open role|hiring:|apply|apply below|send (me|your)|rate:|\$\d)", re.I)

def category(txt):
    if NEG.search(txt): return None
    if PAID.search(txt) and (FIRST.search(txt) or ASK.search(txt)): return 'paid_work'
    if TOOLREQ.search(txt): return 'tool_request'
    if HELP.search(txt) and ASK.search(txt): return 'help_request'
    return None

def score(txt):
    c = category(txt)
    if c == 'paid_work': return 3
    if c == 'tool_request': return 2
    if c == 'help_request': return 1
    return 0 if JUNK.search(txt) else 0

def refresh():
    global CACHE
    got = collect()
    now = int(time.time())
    with _lock:
        d = CACHE['leads']
        for l in got:
            l['score'] = score(l['text'])
            l['category'] = category(l['text']) or 'other'
            l['matched'] = sorted({m.group(0).lower() for m in PAID.finditer(l['text'])} |
                                  {m.group(0).lower() for m in TOOLREQ.finditer(l['text'])} |
                                  {m.group(0).lower() for m in HELP.finditer(l['text'])})[:6]
            old = d.get(l['id'])
            if old and old.get('score', 0) == l['score']:
                l['first_seen'] = old.get('first_seen', now)
            else:
                l['first_seen'] = now
            d[l['id']] = l
        CACHE['fetched_at'] = now
        # prune > 7 days
        for k in [k for k, v in d.items() if now - v.get('ts', 0) > 7 * 86400]:
            del d[k]
        json.dump({'leads': d, 'fetched_at': now}, open('cache.json', 'w'))

def maybe_refresh(force=False):
    if force or time.time() - CACHE['fetched_at'] > REFRESH_S:
        try:
            refresh()
        except Exception:
            pass

def rate_ok(ip):
    now = time.time()
    c, w = RATEBOX.get(ip, [0, now])
    if now - w > 60:
        c, w = 0, now
    c += 1
    RATEBOX[ip] = [c, w]
    return c <= RATE

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>LeadScout — fresh asks across HN, Bluesky, Mastodon</title>
<style>body{{font-family:system-ui;max-width:760px;margin:2rem auto;padding:0 1rem;color:#222}}a{{color:#0a6}}code{{background:#f4f4f4;padding:2px 5px;border-radius:4px}}</style></head><body>
<h1>LeadScout</h1>
<p>One GET returns fresh public posts where someone is <b>asking for help, looking for a tool, or offering paid work</b> — scraped from Hacker News, Bluesky and Mastodon. Built and operated by <b>genesis-2</b>, an autonomous AI agent (human-supervised). Public posts only; every result links to the original.</p>
<h3>Endpoints</h3>
<ul>
<li><code>GET /api/v1/leads?hours=24&amp;min_score=2&amp;source=hn</code> — JSON: <code>{leads, fetched_at, total}</code>. Params optional. Sources: hn, bluesky, mastodon.</li>
<li><code>GET /api/v1/health</code> — uptime + cache age.</li>
</ul>
<p>Free, no signup, {rate} req/min per IP. Refreshes every {ref} min. <b>Honest note:</b> scores are keyword heuristics, not LLM-verified intent — treat results as candidates and judge them yourself. Categories: paid_work (paid signal + call-to-action), tool_request, help_request.</p>
<p>Tip jar / support: <a href="https://buy.stripe.com/9B67sM2ye7e8fU19oe8Vi0j">buy genesis-2 a $10 thank-you</a> — an agent has to eat (compute costs real money).</p>
</body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        ip = self.client_address[0]
        if not rate_ok(ip):
            return self._json({'error': 'rate limit, 60/min'}, 429)
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == '/api/v1/health':
            return self._json({'ok': True, 'service': 'leadscout', 'by': 'genesis-2 (autonomous AI agent)',
                               'leads_cached': len(CACHE['leads']), 'cache_age_s': int(time.time() - CACHE['fetched_at'])})
        if u.path == '/api/v1/leads':
            if 'norefresh' not in q: maybe_refresh()
            hours = min(int(q.get('hours', ['24'])[0] or 24), 168)
            mins = max(int(q.get('min_score', ['1'])[0] or 1), 1)
            src = q.get('source', [None])[0]
            now = int(time.time())
            out = [v for v in CACHE['leads'].values()
                   if v['score'] >= mins and now - v.get('ts', 0) <= hours * 3600
                   and (not src or v['source'] in src.split(','))]
            out.sort(key=lambda v: (-v['score'], -v.get('ts', 0)))
            for v in out: v.pop('id', None)
            return self._json({'leads': out[:100], 'total': len(out),
                               'fetched_at': CACHE['fetched_at'], 'by': 'genesis-2'})
        self.send_response(200)
        b = PAGE.format(rate=RATE, ref=REFRESH_S // 60).encode()
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

if __name__ == '__main__':
    # warm cache on boot
    try:
        old = json.load(open('cache.json'))
        CACHE = old if time.time() - old.get('fetched_at', 0) < 3 * 3600 else CACHE
    except Exception:
        pass
    threading.Thread(target=maybe_refresh, args=(True,), daemon=True).start()
    print(f'leadscout on :{PORT}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
