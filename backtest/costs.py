"""
Transaction-cost model for the backtest.

Zerodha discount-brokerage rates for NSE F&O, as of FY2024-25. These are
approximate and configurable — the goal is to capture the *relative* drag
between instruments, not to reconcile a contract note to the paisa.

Key asymmetry the comparison hinges on:
  - Option costs scale with *premium* turnover  (~₹13k for 1 lot @ ₹200)
  - Future costs scale with *notional* turnover (~₹15.6L for 1 lot @ 24000)
    → futures STT + exchange charges are ~5-8x an option's in rupees.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostConfig:
    brokerage_per_order: float = 20.0        # ₹20 flat per executed order (F&O)

    # Options (charged on premium turnover)
    opt_stt_sell: float = 0.001000           # 0.10% on sell-side premium (Budget 2024)
    opt_exch_txn: float = 0.00035030          # NSE txn charge on premium turnover
    opt_stamp_buy: float = 0.00003            # 0.003% on buy-side premium

    # Futures (charged on notional turnover)
    fut_stt_sell: float = 0.000200           # 0.02% on sell-side notional (Budget 2024)
    fut_exch_txn: float = 0.00001730          # NSE txn charge on notional turnover
    fut_stamp_buy: float = 0.00002            # 0.002% on buy-side notional

    sebi_charges: float = 0.0000010           # ₹10 / crore, both sides
    gst: float = 0.18                         # on brokerage + exch txn + sebi
    slippage_pts: float = 0.0                 # extra points paid on entry+exit (set per instrument)


DEFAULT = CostConfig()


def _gst_base(brokerage: float, exch_txn: float, sebi: float, cfg: CostConfig) -> float:
    return cfg.gst * (brokerage + exch_txn + sebi)


def option_roundtrip(entry_premium: float, exit_premium: float, lot_size: int,
                     cfg: CostConfig = DEFAULT) -> float:
    """Total charges (₹) for buying then selling one option position (1 lot)."""
    buy_val = entry_premium * lot_size
    sell_val = exit_premium * lot_size
    brokerage = 2 * cfg.brokerage_per_order
    stt = cfg.opt_stt_sell * sell_val
    exch = cfg.opt_exch_txn * (buy_val + sell_val)
    sebi = cfg.sebi_charges * (buy_val + sell_val)
    stamp = cfg.opt_stamp_buy * buy_val
    gst = _gst_base(brokerage, exch, sebi, cfg)
    return brokerage + stt + exch + sebi + stamp + gst


def future_roundtrip(entry_price: float, exit_price: float, lot_size: int,
                     cfg: CostConfig = DEFAULT) -> float:
    """Total charges (₹) for one futures round trip (1 lot). Priced on notional."""
    buy_val = entry_price * lot_size
    sell_val = exit_price * lot_size
    brokerage = 2 * cfg.brokerage_per_order
    stt = cfg.fut_stt_sell * sell_val
    exch = cfg.fut_exch_txn * (buy_val + sell_val)
    sebi = cfg.sebi_charges * (buy_val + sell_val)
    stamp = cfg.fut_stamp_buy * buy_val
    gst = _gst_base(brokerage, exch, sebi, cfg)
    return brokerage + stt + exch + sebi + stamp + gst


def slippage_cost(lot_size: int, cfg: CostConfig = DEFAULT) -> float:
    """Points of slippage (entry + exit) converted to rupees."""
    return cfg.slippage_pts * lot_size
