# Design: limit-order entry at the zone proximal

**Status:** proposed, not implemented. Written Aug 29 2026 from `backtest/` findings.
**Owner decision needed before build.**

---

## Problem

The zone strategy computes `entry = zone.proximal`, `SL = distal ∓ buffer`,
`target = entry ± 2·risk`. All of that assumes you get filled **at the proximal**.

You don't. The live flow is:

1. `_scan_core` fires a signal when Nifty LTP is within `ZONE_APPROACH_POINTS`
   (50) of the proximal — i.e. **before** price reaches the zone.
2. Telegram notification sent.
3. Human reads it, taps **Approve** — anywhere from ~10 s to several minutes later.
4. `telegram_handler` → `validate_entry()` (rejects only if Nifty already moved
   > ~20 pts past entry) → `place_options_order()` places a **limit order at the
   current option LTP ± 2** — i.e. wherever the option is *now*.

So the actual entry is 10–50 pts into the move, at a richer premium, with the SL
now much further away relative to the shrunken remaining distance to target. The
2R setup becomes ~1R or worse.

### What the backtest measured (3 years, real data, futures P&L)

| Entry | idx pts / 3y | per trade | median "target" hit | losing "target" exits |
|-------|-------------|-----------|---------------------|----------------------|
| **Limit resting at proximal (0 lag)** | **+366** | +1.1 | **+41.8 pts** | **0 / 84** |
| Market ~2.5 min after signal | −3,941 | −4.9 | +0.8 pts | 267 / 543 |
| Market ~7.5 min after signal | −4,624 | −5.8 | −0.5 pts | 259 / 508 |
| Market ~12.5 min after signal | −5,350 | −6.9 | −3.3 pts | 263 / 477 |

The cliff is between **0 lag and any lag**. Sub-bar "few seconds" barely matters
(−3,941 vs −3,915 at half a bar). It's the 2–5 minutes of human approval that
does the damage — and it converts a small positive edge into a large negative one.

This does **not** by itself make the strategy profitable (costs still exceed the
edge — see `AGENT_KNOWLEDGE.md` §14), but it is the prerequisite for everything
else and stops the largest single leak.

---

## Proposed flow — poll-to-fill limit at the proximal

You cannot place a limit order on the Nifty *index* (not tradeable). Two options:

### Option A — poll the index, fire the option order on touch (recommended)

Fits the existing architecture (`scheduler` already polls every minute).

1. Signal fires → Telegram, as today. Strike/SL/target all frozen from
   `signal.entry = proximal`.
2. On **Approve**: do **not** place an order. Set status `armed` (new state) with
   an `armed_at` timestamp. Reply "Armed — will enter when Nifty reaches
   `<proximal>`".
3. New monitor pass `monitor_armed_entries()` (every 15–30 s, or fold into
   `monitor_open_trades`): for each `armed` signal, read Nifty LTP.
   - **Touch:** `demand` → `LTP <= proximal + tick`; `supply` → `LTP >= proximal − tick`
     (small tolerance, ~1–2 pts, since we want a fill near the line, not exactly on it).
     → call the existing entry path: `get_options_contract(signal.entry, class)`
       → `place_options_order(symbol, "BUY", qty)` (still a limit at option LTP ± 2).
       → status `open`, record fill, notify.
   - **Expired:** `now − armed_at > SIGNAL_EXPIRY_MINUTES` → status `expired`, notify
     "price never returned to the zone — no trade".
   - **Zone broken:** last completed 5-min close beyond `distal` → status `expired`,
     notify "zone broken before entry".
4. `AUTO_FIRST` / `FULLY_AUTOMATED` arm automatically instead of ordering immediately.

**Trade-off:** far fewer trades. Backtest: 333 limit fills vs ~812 market fills
over 3 years — price often doesn't return to the exact proximal. That is correct
behaviour: those skipped trades are the ones with no real edge.

### Option B — pre-place the option limit at the proximal-equivalent premium

Compute the premium the option *should* have when Nifty = proximal (BS from
current IV/DTE, or shift current premium by `delta · (LTP − proximal)`), place a
resting limit there immediately. Kite fills if the option trades to it.

Rejected for v1: premium ≠ linear in index (delta drift, IV moves over the wait),
GTT/limit-far-from-LTP handling is fiddly, and it's less transparent than "we
entered because Nifty touched the line."

---

## Implementation sketch (Option A)

| File | Change |
|------|--------|
| `journal/db.py` | new status `armed`; `arm_signal(id)`, `get_armed_signals()`, `expire_armed(id)`; column `armed_at` |
| `telegram_handler.py` | Approve → `arm_signal()` + "Armed" reply instead of the order block. Keep a manual "Enter now" override button for judgement calls. |
| `scheduler.py` | `monitor_armed_entries()` — touch / expiry / zone-broken checks; on touch run the current entry block (extract it from `telegram_handler` into one shared `place_entry(signal)` function so both paths use it) |
| `scheduler.py` scan | `AUTO_FIRST` / `FULLY_AUTOMATED` → `arm_signal()` not immediate order |
| `config.py` | `ENTRY_TOUCH_TOLERANCE_PTS = 2`; reuse `SIGNAL_EXPIRY_MINUTES` for the armed window |
| `app.py` | Signals tab: show `armed` state + "Enter now" / "Cancel" controls |

Backtest already models this exactly: `backtest/replay.py` `entry_mode="limit"`
(`_entry_fill`). Keep the two in sync.

---

## Edge cases

- **Gap through the proximal** (price opens past it): treat an open beyond the
  line as a touch (you'd have filled). Backtest fills on `low <= proximal <= high`.
- **Touch then immediate reversal:** accepted — you're in at a good price with the
  SL intact; the normal SL handles the reversal.
- **Multiple armed signals, opposite directions:** keep the existing one-position
  rule — arming doesn't reserve, first touch wins, cancel the rest.
- **Approval near `TIME_EXIT_HOUR`:** don't arm within `SIGNAL_EXPIRY_MINUTES` of
  the cutoff (matches backtest).
- **Slippage:** backtest assumes an exact fill at the proximal on any touch —
  mildly optimistic. Real fills will be a tick or two worse; the option limit
  (LTP ± 2) absorbs most of it.

---

## Rollout

1. Build behind `ENTRY_MODE = "armed" | "immediate"` in settings, default
   `immediate` (no behaviour change on deploy).
2. Paper mode for 1 week — compare armed-fill entries vs what immediate would have done.
3. Flip one live day, `AUTO_FIRST` only, watch the fills.
4. Then default `armed`.

Do not ship this same-day with any other live change.
