# Gift Upgrade Tracker — data pack

Prepared 2026-11-10 for Mehrab's gift-upgrade alert bot (all collections; alerts only for
black backdrops + hand-picked models).

powered by @GiftChanges (api.changes.tg) — thanks to @GiftChanges for this API.
Note: any bot/app built on this data source must keep a user-visible credit to
@GiftChanges (e.g. in the bot's /start or /about text, or a footer line).

## What's inside

- `gifts-models-and-black-backdrops.md` — the readable full list: every upgradable
  collection (124), all of its models sorted rarest first with rarity %, and its
  black backdrops with rarity % and colors.
- `models.csv` — same model data as a spreadsheet (gift, slug, model, rarity %, permille, crafted flag/tier).
- `black-backdrops.csv` — one row per collection per black backdrop (248 rows), with rarity and hex colors.
- `config.yaml` — ready-to-fill bot config: one block per collection slug,
  `black_backdrop: true`, and an empty `models: []` list to fill with chosen model names.
- `data/` — raw API responses used to build the above (`gifts.json`, `all-backdrops.json`, `raw/<Gift>.json`).
- `fetch_data.py` / `build_outputs.py` — the scripts that produced this pack (re-runnable to refresh).

## Key findings

- 124 upgradable collections, 7,942 models in total, 80 distinct backdrops overall.
- The black backdrops are exactly two, and both appear in **every** collection:
  - **Black** — center #363738, edge #0e0f0f (the darkest backdrop that exists)
  - **Onyx Black** — center #4d5254, edge #313638
- Their rarity differs per collection (e.g. Plush Pepe: Black 2%, Artisan Brick: Black 1.2%,
  Algorithm Cup: Black 15%) — see `black-backdrops.csv`.
- Closest dark runner-ups (NOT black, excluded by default; add to the config manually if wanted):
  Seal Brown (#664d45/#47362e), Gunmetal (#4c5d63/#2f3b42), Dark Green, Midnight Blue.
  Detection check: by center+edge colour luminance, Black and Onyx Black are the two darkest
  backdrops of all 80, with a clear gap before the rest.
- Model rarities are the *current* upgrade chances reported by the API; Telegram can change
  them over time, so re-run the fetch before the bot goes live and periodically after.

## Tracker bot v1 (prototype, 2026-11-10)

- `tracker.py` — stdlib-only poller: probes the next serial number per collection with
  Bot API 10.4 `getUniqueGift`, filters black backdrops + configured models, posts alerts
  to a channel/group. Verified offline (`--self-test` passes); NOT yet tested live —
  it needs Mehrab's bot token + target chat id, on his computer.
- `tracker.env.example` — copy to `.env`, fill `BOT_TOKEN` + `TARGET_CHAT_ID`.
- `Dockerfile` — for Cloady (deploy from GitHub / Docker image).
- `config-rare.yaml` — starter config: black backdrops + top-3 rarest non-crafted models
  per gift (366 models, 122 gifts). `rarest-models-shortlist.md` is the readable version.
- `config.yaml` (black backdrops only, models empty) is already runnable as-is — no model
  picks are needed to start testing.
- Run order: `python3 tracker.py --self-test` → `--check PlushPepe-1` →
  `--bootstrap` (finds each gift's current head number) → `--dry-run --once` → live loop.

## Correction (2026-11-10): no issued-count in the Bot API

An earlier message said bootstrapping would read `availability_issued` (the total
upgraded per collection) from a known gift. That field does NOT exist in Bot API 10.4's
`UniqueGift` object — it exists only in MTProto's `starGiftUnique`, which a plain bot
token cannot call. Verified against the official Bot API docs (core.telegram.org/bots/api,
`getUniqueGift` + `UniqueGift`, read 2026-11-10): the object carries `gift_id`, `base_name`,
`name`, `number`, `model`, `symbol`, `backdrop` — no counter. `tracker.py` therefore
bootstraps by probing instead: exponential search for an upper bound, then binary search
for the last existing number, using the gift-name format `<Slug>-<number>`
(e.g. `PlushPepe-2187`, confirmed from live `t.me/nft/...` links). Slugs with an
apostrophe/hyphen (`Durov'sCap`, `Jack-in-the-Box`, `B-DayCandle`) are tried both stripped
and as written; `--check` verifies which form Telegram accepts.

## Cloady check (2026-11-10)

Homepage (cloady.com, read 2026-11-10) corroborates part of the shared post: "Free to
start, no card needed", free tier in select regions — Germany (EU1), US East (USE1),
US West (USW1) are marked "Free tier", others "Free trial". It does NOT confirm the
post's specific numbers (1GB RAM / 0.5 CPU / 5GB SSD, 1M requests, 10GB traffic) or the
7-day idle shutdown — those remain unverified claims from the post, so check them in
the Cloady dashboard when creating the service, and pick a Free Tier region, not Trial.
