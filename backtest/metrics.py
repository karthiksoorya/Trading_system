"""
Performance metrics for a list of backtested trades.

A `Trade` is instrument-agnostic: it just needs an entry time, an exit time and
a net rupee P&L (after costs). The replay engine produces these; this module
turns a list of them into a scorecard.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Trade:
    signal_id: int | None
    date: str
    entry_time: datetime
    exit_time: datetime
    direction: str                 # 'demand' | 'supply'
    instrument: str                # 'futures' | 'options_itm1' | ...
    index_entry: float
    index_exit: float
    index_points: float            # signed, in the trade's favour
    gross_pnl: float               # rupees before costs
    costs: float
    net_pnl: float                 # rupees after costs
    exit_reason: str
    meta: dict = field(default_factory=dict)


@dataclass
class Scorecard:
    instrument: str
    n: int
    wins: int
    losses: int
    win_rate: float
    gross_pnl: float
    total_costs: float
    net_pnl: float
    avg_win: float
    avg_loss: float
    expectancy: float              # net rupees per trade
    profit_factor: float
    max_drawdown: float            # rupees, peak-to-trough on the equity curve
    sharpe_daily: float            # annualised, on daily net P&L
    index_points_total: float
    rupees_per_index_point: float  # net_pnl / index_points_total — the conversion efficiency
    best: float
    worst: float

    def as_row(self) -> dict:
        return {
            "instrument": self.instrument, "n": self.n, "win%": round(self.win_rate * 100, 1),
            "net_pnl": round(self.net_pnl), "expectancy": round(self.expectancy),
            "profit_factor": round(self.profit_factor, 2),
            "max_dd": round(self.max_drawdown), "sharpe": round(self.sharpe_daily, 2),
            "idx_pts": round(self.index_points_total), "₹/pt": round(self.rupees_per_index_point, 1),
            "costs": round(self.total_costs),
        }


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 0.0
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    return max_dd


def summarise(trades: list[Trade], instrument: str | None = None) -> Scorecard:
    if instrument:
        trades = [t for t in trades if t.instrument == instrument]
    label = instrument or (trades[0].instrument if trades else "—")
    n = len(trades)
    if n == 0:
        return Scorecard(label, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                         0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    trades = sorted(trades, key=lambda t: t.exit_time)
    nets = [t.net_pnl for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]

    equity, running = [], 0.0
    for x in nets:
        running += x
        equity.append(running)

    # daily aggregation for Sharpe
    by_day: dict[str, float] = {}
    for t in trades:
        by_day[t.date] = by_day.get(t.date, 0.0) + t.net_pnl
    daily = list(by_day.values())
    if len(daily) > 1 and statistics.pstdev(daily) > 0:
        sharpe = statistics.mean(daily) / statistics.pstdev(daily) * math.sqrt(252)
    else:
        sharpe = 0.0

    gross = sum(t.gross_pnl for t in trades)
    costs = sum(t.costs for t in trades)
    net = sum(nets)
    idx_pts = sum(t.index_points for t in trades)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return Scorecard(
        instrument=label, n=n, wins=len(wins), losses=len(losses),
        win_rate=len(wins) / n,
        gross_pnl=gross, total_costs=costs, net_pnl=net,
        avg_win=statistics.mean(wins) if wins else 0.0,
        avg_loss=statistics.mean(losses) if losses else 0.0,
        expectancy=net / n,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        max_drawdown=_max_drawdown(equity),
        sharpe_daily=sharpe,
        index_points_total=idx_pts,
        rupees_per_index_point=(net / idx_pts) if idx_pts else 0.0,
        best=max(nets), worst=min(nets),
    )


def compare_table(trades: list[Trade]) -> list[dict]:
    instruments = sorted({t.instrument for t in trades})
    return [summarise(trades, inst).as_row() for inst in instruments]
