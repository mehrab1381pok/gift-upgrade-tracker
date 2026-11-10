#!/usr/bin/env python3
"""Gift upgrade tracker v1 (prototype, stdlib only — no dependencies).

Polls Telegram Bot API 10.4 `getUniqueGift` for the next serial number of
every collection in config.yaml / config-rare.yaml and posts an alert to a
channel/group when a new upgrade matches the filter:

  * backdrop is Black or Onyx Black, OR
  * model is in the per-gift `models:` list of the config.

Why probing, not a counter?
  Bot API 10.4's UniqueGift object has NO total-issued counter (no
  `availability_issued` — that field exists only in MTProto's starGiftUnique,
  not in the Bot API). So the tracker finds the current head of each
  collection by probing: exponential search for an upper bound, then binary
  search for the last existing number. See `--bootstrap`.
  Gift names are `<Slug>-<number>`, e.g. `PlushPepe-2187` (the part after
  https://t.me/nft/). Slugs with an apostrophe / hyphen (Durov's Cap,
  Jack-in-the-Box, B-Day Candle, ...) are tried both stripped
  (`DurovsCap-1`) and as written (`Durov'sCap-1`); the variant that works
  is remembered in state.json. Verify with `--check DurovsCap-1`.

Data credit: the model/backdrop lists in config*.yaml were built from the
free @GiftChanges API (api.changes.tg). The tracker itself only calls the
official Telegram Bot API at runtime. If you publish the bot, keep a visible
credit to @GiftChanges (e.g. in the bot's /about text on BotFather).

Usage (on your computer, not your phone):
  cp tracker.env.example .env        # fill BOT_TOKEN + TARGET_CHAT_ID
  python3 tracker.py --self-test     # offline check, no token needed
  python3 tracker.py --check PlushPepe-1
  python3 tracker.py --bootstrap     # one-off: find the head of each gift
  python3 tracker.py --dry-run --once
  python3 tracker.py                 # the real loop (POLL_SECONDS between cycles)

Env: BOT_TOKEN, TARGET_CHAT_ID, CONFIG_PATH (default config.yaml),
     STATE_PATH (default state.json), POLL_SECONDS (default 90).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api.telegram.org/bot{token}/{method}"
BLACK_BACKDROPS = {"Black", "Onyx Black"}
# (center, edge) RGB ints for the two black backdrops, from black-backdrops.csv
BLACK_COLORS = {(0x363738, 0x0E0F0F), (0x4D5254, 0x313638)}
MAX_NUMBER = 1_000_000          # safety cap for bootstrap probing
CATCHUP_CAP = 200               # max new gifts processed per gift per cycle


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------- config
def parse_config(path):
    """Parse our simple config format (slug blocks, no PyYAML needed).

    Returns {slug: {"black_backdrop": bool, "models": [str, ...]}}.
    models: may be [] or ["A", "B"] (double-quoted, names may contain ').
    """
    cfg = {}
    slug = None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = re.match(r"^(\S[^:]*):\s*$", line)
            if m and not line.startswith(" "):
                slug = m.group(1).strip()
                cfg[slug] = {"black_backdrop": True, "models": []}
                continue
            if slug is None:
                continue
            s = line.strip()
            if s.startswith("black_backdrop:"):
                cfg[slug]["black_backdrop"] = s.split(":", 1)[1].strip().lower() == "true"
            elif s.startswith("models:"):
                rest = s.split(":", 1)[1].strip()
                rest = rest.split("#", 1)[0].strip()  # drop trailing comment
                cfg[slug]["models"] = re.findall(r'"([^"]*)"', rest)
    return cfg


def slug_candidates(slug):
    """Name variants to try for a gift slug. Stripped first (most likely)."""
    stripped = re.sub(r"[^A-Za-z0-9]", "", slug)
    out = []
    for cand in (stripped, slug):
        if cand not in out:
            out.append(cand)
    return out


# ---------------------------------------------------------------- state
def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    # First start on a fresh container: seed from a pre-bootstrapped
    # seed_state.json shipped next to this script, if one exists.
    seed = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "seed_state.json")
    if os.path.exists(seed):
        log("no state file yet — seeding from seed_state.json")
        with open(seed, encoding="utf-8") as fh:
            return json.load(fh)
    return {"version": 1, "gifts": {}}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- bot api
class Bot:
    def __init__(self, token):
        self.token = token

    def call(self, method, params=None, _retries=5):
        url = API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {}
            if e.code == 429 and _retries > 0:
                wait = int((body.get("parameters") or {}).get("retry_after", 5))
                log(f"  429 rate-limited, waiting {wait}s")
                time.sleep(wait + 1)
                return self.call(method, params, _retries - 1)
            return {"ok": False, "error_code": e.code, **body}
        except Exception as e:  # network blip: caller decides
            return {"ok": False, "error_code": 0, "description": f"network: {e}"}

    def get_unique_gift(self, name, _attempt=0):
        """Returns UniqueGift dict, None if the gift does not exist (yet).
        Network errors and 5xx are retried with backoff — a timeout must
        never be mistaken for 'gift does not exist'."""
        res = self.call("getUniqueGift", {"name": name})
        if res.get("ok"):
            return res["result"]
        desc = str(res.get("description", ""))
        code = res.get("error_code")
        if code == 401:
            raise SystemExit("Bot API 401 — BOT_TOKEN is wrong or revoked.")
        if code in (400, 404) or "not found" in desc.lower():
            return None
        if _attempt < 4:
            wait = (2, 5, 10, 20)[_attempt]
            log(f"  getUniqueGift({name}) error {code} ({desc[:60]}), retry in {wait}s")
            time.sleep(wait)
            return self.get_unique_gift(name, _attempt + 1)
        log(f"  getUniqueGift({name}) giving up after retries: {res}")
        return None

    def send_message(self, chat_id, text):
        res = self.call("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })
        if not res.get("ok"):
            log(f"  sendMessage failed: {res}")
        return bool(res.get("ok"))


# ---------------------------------------------------------------- filter
def is_black_backdrop(gift):
    bd = gift.get("backdrop") or {}
    if bd.get("name") in BLACK_BACKDROPS:
        return True
    colors = bd.get("colors") or {}
    pair = (colors.get("center_color"), colors.get("edge_color"))
    return pair in BLACK_COLORS


def matches(gift, rule):
    """rule: {"black_backdrop": bool, "models": [...]}. OR logic."""
    if rule.get("black_backdrop") and is_black_backdrop(gift):
        return True
    model = (gift.get("model") or {}).get("name")
    return bool(model) and model in set(rule.get("models") or [])


def format_alert(gift):
    name = gift.get("name", "?")
    base = gift.get("base_name", "?")
    num = gift.get("number", "?")
    model = (gift.get("model") or {}).get("name", "?")
    model_r = (gift.get("model") or {}).get("rarity_per_mille")
    symbol = (gift.get("symbol") or {}).get("name", "?")
    bd = (gift.get("backdrop") or {}).get("name", "?")
    bd_r = (gift.get("backdrop") or {}).get("rarity_per_mille")
    lines = [
        f"🎁 <b>{base} #{num}</b>",
        f"Model: {model}" + (f" ({model_r/10:.1f}%)" if isinstance(model_r, int) else ""),
        f"Backdrop: {bd}" + (f" ({bd_r/10:.1f}%)" if isinstance(bd_r, int) else ""),
        f"Symbol: {symbol}",
        f"https://t.me/nft/{name}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- probing
def gift_exists(bot, slug, number, variant_cache):
    """Try name variants; remember which variant works for this slug."""
    cands = slug_candidates(slug)
    order = [variant_cache] + [i for i in range(len(cands)) if i != variant_cache] \
        if variant_cache is not None else list(range(len(cands)))
    for i in order:
        g = bot.get_unique_gift(f"{cands[i]}-{number}")
        if g is not None:
            return g, i
    return None, variant_cache


def find_head(bot, slug, variant=None, sleep=0.2):
    """Last existing serial number for a slug (binary search). 0 if none."""
    g, variant = gift_exists(bot, slug, 1, variant)
    if g is None:
        return 0, variant
    lo, hi = 1, 2
    while hi <= MAX_NUMBER:
        g, variant = gift_exists(bot, slug, hi, variant)
        if g is None:
            break
        lo = hi
        hi *= 2
        time.sleep(sleep)
    else:
        return lo, variant
    # lo exists, hi does not: binary search
    while hi - lo > 1:
        mid = (lo + hi) // 2
        g, variant = gift_exists(bot, slug, mid, variant)
        if g is not None:
            lo = mid
        else:
            hi = mid
        time.sleep(sleep)
    return lo, variant


def bootstrap_gifts(bot, state, state_path, slugs, workers=6):
    """Find and record the head number for each slug, in parallel."""
    lock = threading.Lock()

    def _boot(slug):
        gs = state.setdefault("gifts", {}).setdefault(
            slug, {"last_seen": 0, "variant": None})
        head, variant = find_head(bot, slug, gs.get("variant"))
        with lock:
            gs["last_seen"], gs["variant"] = head, variant
            save_state(state_path, state)
        log(f"  {slug}: head=#{head}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_boot, list(slugs)))


def poll_once(bot, cfg, state, chat_id, dry_run, sleep=0.2):
    """One cycle: check next number(s) of every gift. Returns alerts sent."""
    sent = 0
    gifts_state = state.setdefault("gifts", {})
    for slug, rule in cfg.items():
        gs = gifts_state.setdefault(slug, {"last_seen": 0, "variant": None})
        last = int(gs.get("last_seen", 0))
        variant = gs.get("variant")
        n = last + 1
        checked = 0
        while checked < CATCHUP_CAP:
            g, variant = gift_exists(bot, slug, n, variant)
            if g is None:
                break
            gs["variant"] = variant
            last = n
            checked += 1
            if matches(g, rule):
                text = format_alert(g)
                if dry_run:
                    log(f"  [dry-run] would post:\n{text}")
                elif bot.send_message(chat_id, text):
                    sent += 1
                    time.sleep(1.1)  # stay under Telegram's ~1 msg/sec per chat
                log(f"  ✅ {g.get('name')} model={g.get('model', {}).get('name')} "
                    f"backdrop={g.get('backdrop', {}).get('name')}")
            n += 1
            time.sleep(sleep)
        gs["last_seen"] = last
    return sent


# ---------------------------------------------------------------- env
def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def self_test():
    """Offline checks: config parsing, slug variants, filter, formatting."""
    cfg = parse_config(os.environ.get("CONFIG_PATH", "config.yaml"))
    assert len(cfg) == 124, f"expected 124 gifts, got {len(cfg)}"
    assert cfg["PlushPepe"]["black_backdrop"] is True
    assert cfg["PlushPepe"]["models"] == [], "config.yaml models must be empty (black-only)"
    rare = parse_config("config-rare.yaml")
    assert rare["ArtisanBrick"]["models"] == ["Gold Bar", "Gold Block", "USBrick"], rare["ArtisanBrick"]["models"]
    assert rare["AlgorithmCup"]["models"] == []
    assert slug_candidates("Durov'sCap") == ["DurovsCap", "Durov'sCap"]
    assert slug_candidates("PlushPepe") == ["PlushPepe"]
    black = {"name": "PlushPepe-1", "base_name": "Plush Pepe", "number": 1,
             "model": {"name": "Cozy Galaxy", "rarity_per_mille": 10},
             "symbol": {"name": "Star"}, "backdrop": {"name": "Black", "rarity_per_mille": 20,
             "colors": {"center_color": 0x363738, "edge_color": 0x0E0F0F}}}
    assert is_black_backdrop(black) is True
    assert matches(black, cfg["PlushPepe"]) is True
    plain = dict(black, backdrop={"name": "Red", "rarity_per_mille": 50,
                 "colors": {"center_color": 0xFF0000, "edge_color": 0x880000}})
    assert matches(plain, cfg["PlushPepe"]) is False
    assert matches(plain, {"black_backdrop": True, "models": ["Cozy Galaxy"]}) is True
    txt = format_alert(black)
    assert "Plush Pepe #1" in txt and "https://t.me/nft/PlushPepe-1" in txt, txt
    print(f"self-test OK: {len(cfg)} gifts parsed, filter/format/slugs fine")
    print("sample alert:\n" + txt)


def start_health_server():
    """Tiny HTTP listener so PaaS hosts (e.g. Cloady) see an open port and
    external pingers can keep the service warm. Health response only."""
    port = int(os.environ.get("PORT", "8080"))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"health server listening on :{port}")
    except Exception as e:
        log(f"health server not started: {e!r}")


def main():
    ap = argparse.ArgumentParser(description="Telegram gift upgrade tracker v1")
    ap.add_argument("--config", default=None)
    ap.add_argument("--state", default=None)
    ap.add_argument("--check", metavar="NAME", help="fetch one gift, e.g. PlushPepe-1")
    ap.add_argument("--bootstrap", action="store_true", help="find head number per gift")
    ap.add_argument("--bootstrap-gift", metavar="SLUG", help="bootstrap one gift only")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    cfg_path = args.config or os.environ.get("CONFIG_PATH", "config.yaml")
    state_path = args.state or os.environ.get("STATE_PATH", "state.json")

    if args.self_test:
        os.chdir(os.path.dirname(os.path.abspath(__file__)) or ".")
        return self_test()

    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise SystemExit("BOT_TOKEN is not set — copy tracker.env.example to .env and fill it in.")
    bot = Bot(token)
    chat_id = os.environ.get("TARGET_CHAT_ID", "")

    if args.check:
        g = bot.get_unique_gift(args.check)
        print(json.dumps(g, ensure_ascii=False, indent=1) if g else f"not found (yet): {args.check}")
        return

    cfg = parse_config(cfg_path)
    log(f"loaded {len(cfg)} gifts from {cfg_path}")
    state = load_state(state_path)

    if args.bootstrap or args.bootstrap_gift:
        targets = [args.bootstrap_gift] if args.bootstrap_gift else list(cfg)
        workers = int(os.environ.get("BOOTSTRAP_WORKERS", "6"))
        bootstrap_gifts(bot, state, state_path, targets, workers)
        log(f"bootstrap done, state saved to {state_path}")
        return

    # Health server first: the host marks the service healthy and external
    # pingers can reach it while bootstrap is still running.
    start_health_server()

    # Auto-bootstrap: any gift with no recorded head must be bootstrapped
    # before polling, otherwise the loop would walk from #1 and re-post
    # historical matches. Runs in parallel; also the fallback on a fresh
    # container when no seed_state.json was shipped.
    missing = [s for s in cfg
               if not state.get("gifts", {}).get(s, {}).get("last_seen")]
    if missing and not args.dry_run:
        workers = int(os.environ.get("BOOTSTRAP_WORKERS", "6"))
        log(f"auto-bootstrap: finding head for {len(missing)} gifts "
            f"({workers} workers)...")
        bootstrap_gifts(bot, state, state_path, missing, workers)
        log("auto-bootstrap done")

    if not chat_id and not args.dry_run:
        raise SystemExit("TARGET_CHAT_ID is not set (or use --dry-run).")
    poll_seconds = float(os.environ.get("POLL_SECONDS", "90"))
    while True:
        try:
            sent = poll_once(bot, cfg, state, chat_id, args.dry_run)
            save_state(state_path, state)
            log(f"cycle done, alerts sent: {sent}")
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"cycle error (state kept, will retry): {e!r}")
        if args.once:
            break
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
