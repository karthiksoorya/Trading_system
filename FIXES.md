# Trading System — Bug Fix Log

All bugs identified, fixed, and pushed to GitHub across two review rounds.

---

## Round 1 — Software Bug Fixes

### Critical (Financial Risk)

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | `scheduler.py`, `app.py`, `telegram_handler.py` | SELL exit used live `get_lot_size()` instead of stored entry lot size — risk of naked short if NSE revises lot size mid-position | Use `options_lot_size` from DB row at exit time; fallback to live only if missing |
| 2 | `scheduler.py`, `telegram_handler.py` | No lock between `monitor_open_trades` (1-min loop) and Telegram close handler — simultaneous exit could place two SELL orders | Added `_exit_lock` threading lock; `_live_exit` checks DB status before placing order |
| 3 | `scheduler.py` | Breakeven SL applied and exit check ran in same tick — could trigger immediate SL close the moment breakeven fired | Added `breakeven_just_applied` flag; skips exit check for that tick |
| 10 | `journal/db.py` | `close_trade()` had no guard against being called twice — double-counted P&L in `daily_summary` | Added `if entry_row["status"] == "closed": return` before processing |

### High Severity

| # | File | Issue | Fix |
|---|------|-------|-----|
| 4 | `journal/db.py` | `log_signal()` used stale module-level `config.MODE` — trades logged with wrong mode after live/paper switch | Changed to `config.load_settings().get("MODE", config.MODE)` |
| 5 | `scheduler.py` | `every().day.at(SCAN_START)` + `every(5).minutes` both fired at 10:05 — double scan, potential duplicate signals | Removed the `every().day.at(SCAN_START)` schedule line |
| 6 | `app.py` | Download CSV button looked for `{date}.csv` but `export.py` writes `trades_{date}.csv` — button always showed disabled | Fixed filename to `trades_{date.today().isoformat()}.csv` |
| 7 | `app.py` | `import collections` inside Zone Type `try` block — Timeframe Performance section below it caused `NameError` if block threw | Added `import collections` locally inside the Timeframe Performance block |
| 11 | `scheduler.py` | `end_of_day()` scheduled at 15:20 AND called again at 15:35 auto-stop — two EOD Telegram summaries per day | 15:35 call now only runs `end_of_day()` if open trades still exist |

### Medium Severity

| # | File | Issue | Fix |
|---|------|-------|-----|
| 8 | `engine/zones.py` | `_build_zone()` excluded `leg_in` from distal pool for RBR (demand) and DBD (supply) — SL placed too tight | `leg_in` always included in distal pool for all 4 zone types |
| 9 | `engine/zones.py` | `detect_zones()` set `i = j` after zone found — adjacent zones shared a candle (overlapping zones) | Changed to `i = j + 1` to start fresh after each zone's leg_out |
| 12 | `app.py` | Learning tab SQL queries used f-string interpolation — SQL injection risk | Replaced with `_learn_where_sql` + `_learn_where_args` parameterized queries |
| 13 | `scheduler.py` | `config.load_settings()` called 3+ times during same scan — inconsistent settings if dashboard writes mid-scan | Single `_s = config.load_settings()` at scan start; all values read from `_s` |
| 14 | `engine/boosters.py` | `score_strength()` compared `leg_out.open` against current live candle — gap almost always True, every zone scored 2.0 | Gap now compared against `zone.base_candles[-1].close` (candle before leg_out) |
| 15 | `engine/zones.py` | `update_zone_state()` only counted touches via wick extreme — body-inside-zone candles not counted | Touch now triggered if wick OR candle body enters the zone |
| 16 | `telegram_handler.py` | `approve_signal()` called before Kite order placed — `monitor_open_trades` could see approved trade with no real entry | `approve_signal()` now called only after order is confirmed placed |
| 17 | `app.py` | Tutorial tab showed lot size as 75 — config correctly had 65 (NSE revised Jan 2026) | Corrected tutorial text to 65 units (verified via NSE/Zerodha sources) |
| 18 | `scheduler.py` | `check_pending_freshness()` expired signals when `ltp <= proximal` — too aggressive, expired valid signals far from zone | Touch condition tightened: `distal <= ltp <= proximal` (must be inside zone) |
| 19 | `journal/db.py` | `zone_signaled_today()` used exact float equality on proximal — epsilon differences allowed duplicate signals | Changed to `BETWEEN proximal - 0.01 AND proximal + 0.01` |

### Low Severity

| # | File | Issue | Fix |
|---|------|-------|-----|
| 20 | `engine/signals.py` | `Signal.is_tradeable` used stale module-level `MIN_BOOSTER_SCORE` | Now reads via `config.load_settings()` |
| 21 | `journal/db.py` | `_migrate()` did not backfill NULL status rows after `ALTER TABLE` | Added `UPDATE signals SET status='pending' WHERE status IS NULL` |
| 22 | `autolearn.py` | Auto-learn analysed full trade history — old regime data suppressed currently-profitable zone types | Added 90-day recency window (`date >= cutoff`) to the query |

---

## Round 2 — Trading Logic Fixes

Identified by re-reading the code from a **stock market analyst / options trading** perspective.

| # | File | Issue | Fix |
|---|------|-------|-----|
| A | `config.py` | `CAPITAL = ₹10,000` — risk per trade was ₹25, far below the cost of one Nifty lot (₹3k–₹8k premium). Position sizing was decorative. | Updated `CAPITAL = ₹1,00,000` (1 lakh). Daily loss limit now ₹1,000. Live mode still trades exactly 1 lot. |
| B | `brokers/kite_adapter.py` | Only same-day expiry was skipped. A contract expiring tomorrow (1 day away) has extreme gamma risk and wide spreads — nearly as bad as same-day. | Changed condition from `days_ahead == 0` to `days_ahead <= 1`; jumps to following week when within 2 days of expiry. |
| C | `engine/signals.py` | `overnight_target` stored and shown in UI but never acted on — engine always closes intraday at 15:20. Misleading to show as a live target. | Documented as informational-only with clear comments. No code path switches to overnight target automatically. |
| D | `engine/signals.py`, `scheduler.py` | Intraday target was always set at 2× risk regardless of nearby resistance/support. Target was often placed inside an opposing zone that would absorb it. | `generate_signal()` now accepts `opposing_zones`. If a valid opposing zone's proximal sits between entry and the 2× target, intraday target is capped 2 pts before it. |
| E | `engine/confluence.py` | `_zones_overlap()` only checked if entry zone's proximal fell inside the reference zone — missed cases where bands partially overlap and allowed false-single-point touches. | Changed to full band overlap check: `entry_low <= ref_high and entry_high >= ref_low`. |
| F | `scheduler.py` | Daily loss guard used `config.MAX_DAILY_LOSS` (computed once at import from `CAPITAL × MAX_RISK_PCT`). If capital was changed in settings, the guard used the old value. | Now computes `_max_daily_loss = _capital × _max_risk_pct` from `load_settings()` at scan time. |
| G | `config.py` | `SCAN_START = "10:05"` — catches zones formed during opening volatility (09:15–10:00) which are less reliable. | Changed to `"10:15"` to allow the market to settle before the first scan. |
| H | `config.py`, `scheduler.py` | No India VIX check. High VIX (>20) inflates option premiums — buying ATM CE/PE in high-VIX environment means immediate adverse theta impact. | Added `VIX_MAX = 20.0` to config. `_scan_core()` fetches VIX at scan start; skips the entire scan if VIX > threshold. |

---

## Commit History

| Commit | Description |
|--------|-------------|
| `074d38d` | Fix critical bugs 1, 2, 3, 10 |
| `62f8639` | Fix high severity bugs 4, 5, 6, 7, 11 |
| `9af7c93` | Fix medium bugs 14, 16 |
| `73d4bc6` | Fix medium bugs 8, 9 |
| `3038d03` | Fix medium bugs 12, 13, 15, 17, 18, 19 |
| `2f759f1` | Fix low severity bugs 20, 21, 22 |
| `v1.1.0`  | Fix trading logic issues A–H + create FIXES.md |
