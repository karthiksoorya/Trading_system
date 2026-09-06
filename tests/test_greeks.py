from agents.greeks import calculate_greeks, implied_volatility
from agents.completed_trade_reflection import reflect_trade
import json

def test_known_iv_solver_and_ce_greeks():
    snap=calculate_greeks(10,100,100,"2026-12-31","2026-09-06T10:00:00","CE")
    assert snap and 0 < snap.iv < 8 and 0 < snap.delta < 1 and snap.gamma >= 0
    assert snap.vega > 0 and snap.theta < 0

def test_pe_delta_negative_and_failure_unknown():
    snap=calculate_greeks(10,100,100,"2026-12-31","2026-09-06T10:00:00","PE")
    assert snap and -1 < snap.delta < 0 and snap.gamma >= 0
    assert calculate_greeks(0,100,100,"2026-12-31","2026-09-06T10:00:00","CE") is None

def test_missing_inputs_and_expired_return_unknown():
    assert calculate_greeks(10,None,100,"2026-01-01","2026-09-06T10:00:00","CE") is None
    assert calculate_greeks(10,100,100,"2026-01-01","2026-09-06T10:00:00","CE") is None

def test_reflection_uses_signal_proxy_basis_when_exact_fill_missing():
    row={"id":1,"status":"closed","date":"2026-09-06","time_signal":"10:00:00",
    "options_symbol":"NIFTY2690824050CE","entry":24000,"pnl_points":1,
    "options_entry_price":100,"options_exit_price":110,"options_lot_size":65}
    d=json.loads(reflect_trade(row).evidence[0])
    assert d["greeks_basis_entry"] == "signal_time_proxy_model"
    assert d["greeks_timestamp_entry"] == "2026-09-06T10:00:00"
    assert d["theta_damage"] == d["iv_crush"] == d["low_delta"] == "UNKNOWN"
