from agents.greeks import calculate_greeks, implied_volatility
from agents.completed_trade_reflection import reflect_trade
import json
import pytest
import agents.greeks as greeks

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


@pytest.mark.parametrize('option_type', ['CE', 'PE'])
@pytest.mark.parametrize('underlying', [80, 100, 120])
def test_theta_matches_finite_difference_price(option_type, underlying):
    expiry, as_of = '2026-12-31', '2026-09-26T10:00:00+05:30'
    t = greeks.time_to_expiry(expiry, as_of)
    call = option_type == 'CE'
    price = greeks._price(underlying, 100, t, .06, 0, .3, call)
    snap = greeks.calculate_greeks(price, underlying, 100, expiry, as_of, option_type)
    h = 1e-5
    theta = (greeks._price(underlying, 100, t-h, .06, 0, .3, call) -
             greeks._price(underlying, 100, t+h, .06, 0, .3, call)) / (2*h*365)
    assert snap.theta == pytest.approx(theta, abs=1e-6)


def test_expiry_ist_and_utc_are_equivalent():
    assert greeks.time_to_expiry('2026-09-29', '2026-09-26T10:00:00+05:30') == greeks.time_to_expiry('2026-09-29', '2026-09-26T04:30:00Z')


def test_put_theta_dividend_sign(monkeypatch):
    monkeypatch.setattr(greeks, 'DIVIDEND_YIELD', .02)
    expiry, as_of = '2026-12-31', '2026-09-26T10:00:00+05:30'
    t = greeks.time_to_expiry(expiry, as_of)
    premium = greeks._price(100, 100, t, .06, .02, .3, False)
    snap = greeks.calculate_greeks(premium, 100, 100, expiry, as_of, 'PE')
    h = 1e-5
    expected = (greeks._price(100, 100, t-h, .06, .02, .3, False) - greeks._price(100, 100, t+h, .06, .02, .3, False)) / (2*h*365)
    assert snap.theta == pytest.approx(expected, abs=1e-6)
