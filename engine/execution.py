"""Durable execution service shared by scheduler, dashboard and Telegram.

SQLite claims serialize submissions across processes. An ambiguous submission is
never retried automatically: reconcile its broker order ID before further action.
"""
import logging
import math
import time
from datetime import datetime

import config
from brokers.base import IST, broker_time, EntryValidationRejected
from engine.entry_validation import validate_entry
from journal import db

logger = logging.getLogger(__name__)
TERMINAL = ('COMPLETE', 'CANCELLED', 'REJECTED')


def _now():
    return datetime.now(IST).isoformat()


def _poll_wait():
    time.sleep(1)


def _broker(trade, supplied=None):
    name = trade.get('execution_broker')
    if not name:
        raise ValueError('Broker identity missing; reconcile legacy trade first')
    if supplied is not None:
        if supplied.broker_name != name:
            raise ValueError('Broker does not match recorded trade')
        return supplied
    if name == 'kite':
        from brokers.kite_adapter import KiteAdapter
        return KiteAdapter()
    if name == 'upstox':
        from brokers.upstox_adapter import UpstoxAdapter
        return UpstoxAdapter()
    raise ValueError('Unknown execution broker')


def check_daily_loss(settings=None):
    settings = config.load_settings() if settings is None else settings
    mode = settings.get('MODE', config.MODE)
    if mode != 'live':
        # Index-only paper records have no option rupee accounting.
        return
    account = db.daily_rupee_accounting(mode=mode)
    if not account['complete']:
        raise ValueError('Execution accounting incomplete; reconcile before buying')
    limit = float(settings.get('CAPITAL', config.CAPITAL)) * float(settings.get('MAX_RISK_PCT', config.MAX_RISK_PCT))
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError('Invalid daily loss limit')
    if account['net'] <= -limit:
        raise ValueError(f"Daily loss limit reached: ₹{account['net']:.2f} ({account['basis']})")


def submit_entry(signal_id, broker=None, requester='system'):
    settings = config.load_settings()
    trade = dict(db.get_signal(signal_id) or {})
    if not trade or trade['status'] != 'pending':
        raise ValueError('Signal is not pending')
    if settings.get('MODE', config.MODE) != 'live':
        with db._conn() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT id FROM signals WHERE status IN ('approved','entry_pending','exit_pending','reconciliation_required')").fetchone():
                raise ValueError('Another trade is active')
            con.execute("UPDATE signals SET status='approved', mode='paper' WHERE id=? AND status='pending'", (signal_id,))
        return dict(db.get_signal(signal_id))
    trade['execution_broker'] = settings.get('BROKER', config.BROKER)
    adapter = _broker(trade, broker)
    if not adapter.is_connected():
        raise ValueError('Broker is not connected')
    contract = adapter.get_options_contract(trade['entry'], trade['zone_class'])
    quantity = int(contract['lot_size'])
    if quantity <= 0:
        raise ValueError('Invalid contract quantity')
    # Fetch after contract selection, which may be slow.
    quote = adapter.get_underlying_observation(config.NIFTY_SYMBOL)
    validate_entry(trade, quote, settings)
    check_daily_loss(settings)
    with db._conn() as con:
        con.execute('BEGIN IMMEDIATE')
        if con.execute("SELECT id FROM signals WHERE status IN ('approved','entry_pending','exit_pending','reconciliation_required')").fetchone():
            raise ValueError('Another trade or unresolved order is active')
        # Recheck after acquiring the cross-process claim, before the order is sent.
        check_daily_loss(settings)
        updated = con.execute("UPDATE signals SET status='entry_pending', mode='live', execution_broker=?, "
                              "execution_state='SUBMITTING', options_symbol=?, options_lot_size=? "
                              "WHERE id=? AND status='pending'",
                              (trade['execution_broker'], contract['symbol'], quantity, signal_id)).rowcount
        if not updated:
            raise ValueError('Entry already claimed')
        validate_entry(trade, quote, settings)
        oid = con.execute("INSERT INTO execution_orders (signal_id,broker,side,requested_quantity,status,requested_at,requester) "
                          "VALUES (?,?,'BUY',?,'SUBMITTING',?,?)",
                          (signal_id, trade['execution_broker'], quantity, _now(), requester)).lastrowid
    return _submit(signal_id, oid, adapter, contract['symbol'], 'BUY', quantity)


def _submit(signal_id, local_order_id, adapter, symbol, side, quantity):
    def validate_before_send():
        try:
            trade = dict(db.get_signal(signal_id))
            settings = config.load_settings()
            if settings.get('MODE', config.MODE) != 'live' or settings.get('BROKER', config.BROKER) != trade['execution_broker']:
                raise ValueError('Execution settings changed before submission')
            # Refresh after option-price lookup, just before the broker POST.
            validate_entry(trade, adapter.get_underlying_observation(config.NIFTY_SYMBOL), settings)
        except Exception as exc:
            raise EntryValidationRejected(str(exc)) from exc
    try:
        broker_id = adapter.place_options_order(symbol, side, quantity,
                                                before_submit=validate_before_send if side == 'BUY' else None)
        if not broker_id:
            raise ValueError('Broker returned no order ID')
        with db._conn() as con:
            con.execute("UPDATE execution_orders SET broker_order_id=?, status='PENDING' WHERE id=?",
                        (str(broker_id), local_order_id))
            col = 'kite_order_id' if side == 'BUY' else 'options_exit_order_id'
            con.execute(f"UPDATE signals SET {col}=?, execution_state='PENDING' WHERE id=?", (str(broker_id), signal_id))
    except EntryValidationRejected as exc:
        with db._conn() as con:
            con.execute("UPDATE execution_orders SET status='REJECTED', error=? WHERE id=?", (str(exc), local_order_id))
            con.execute("UPDATE signals SET status='rejected', execution_state='VALIDATION_REJECTED', notes=? WHERE id=?",
                        (str(exc), signal_id))
        return dict(db.get_signal(signal_id))
    except Exception as exc:
        with db._conn() as con:
            con.execute("UPDATE execution_orders SET status='UNKNOWN', error=? WHERE id=?", (str(exc), local_order_id))
            con.execute("UPDATE signals SET status='reconciliation_required', execution_state='UNKNOWN' WHERE id=?", (signal_id,))
        logger.exception('Order submission ambiguous for signal %s; no automatic retry', signal_id)
        return dict(db.get_signal(signal_id))
    # Most limit fills arrive after the placement response. Observe them promptly
    # instead of waiting for the one-minute risk monitor and losing fill context.
    result = reconcile_trade(signal_id, adapter)
    for _ in range(4):
        if result['status'] not in ('entry_pending', 'exit_pending'):
            break
        _poll_wait()
        result = reconcile_trade(signal_id, adapter)
    return result


def bind_broker_order(local_order_id, broker_order_id):
    """Explicit recovery after the broker confirms an ambiguous submission's ID.

    Does not place orders. Broker details are verified on the next reconciliation.
    """
    if not str(broker_order_id).strip():
        raise ValueError('Broker order ID is required')
    with db._conn() as con:
        order = con.execute('SELECT * FROM execution_orders WHERE id=?', (local_order_id,)).fetchone()
        if not order:
            raise ValueError('Unknown local order')
        order = dict(order)
    trade = dict(db.get_signal(order['signal_id']))
    snapshot = _broker(trade).get_order_execution(str(broker_order_id))
    if (snapshot.order_id != str(broker_order_id) or snapshot.side != order['side']
            or snapshot.symbol != trade['options_symbol'] or snapshot.requested_quantity != order['requested_quantity']):
        raise ValueError('Broker order does not match the recorded submission')
    with db._conn() as con:
        updated = con.execute("UPDATE execution_orders SET broker_order_id=?, status='PENDING', error=NULL "
                              "WHERE id=? AND broker_order_id IS NULL AND status IN ('UNKNOWN','SUBMITTING')",
                              (str(broker_order_id), local_order_id)).rowcount
        if not updated:
            raise ValueError('Order is not awaiting identity reconciliation')


def request_exit(signal_id, reason='manual', requester='system', broker=None, index_price=None, notes=''):
    trade = dict(db.get_signal(signal_id) or {})
    if not trade:
        raise ValueError('Unknown trade')
    if trade['status'] == 'closed':
        return trade
    if trade['mode'] != 'live':
        if index_price is None:
            raise ValueError('Paper close requires an observed index price')
        db.close_trade(signal_id, index_price, reason, notes, closed_by=requester)
        return dict(db.get_signal(signal_id))
    adapter = _broker(trade, broker)
    trade = reconcile_trade(signal_id, adapter)
    if trade['status'] == 'closed':
        return trade
    with db._conn() as con:
        con.execute("UPDATE signals SET exit_reason=?, closed_by=? WHERE id=? AND status!='closed'",
                    (reason, requester, signal_id))
    for order in db.execution_orders(signal_id):
        if order['side'] == 'BUY' and order['status'] not in TERMINAL:
            if order['broker_order_id']:
                adapter.cancel_execution_order(order['broker_order_id'])
            # Do not SELL until the broker confirms cancellation and final BUY quantity.
            return reconcile_trade(signal_id, adapter)
    with db._conn() as con:
        con.execute('BEGIN IMMEDIATE')
        orders = list(con.execute('SELECT * FROM execution_orders WHERE signal_id=?', (signal_id,)))
        if not orders:
            raise ValueError('Legacy live position requires broker execution reconciliation')
        if any(o['status'] not in TERMINAL for o in orders):
            return dict(con.execute('SELECT * FROM signals WHERE id=?', (signal_id,)).fetchone())
        current = dict(con.execute('SELECT * FROM signals WHERE id=?', (signal_id,)).fetchone())
        if current['status'] == 'closed':
            return current
        quantity = (current['options_entry_quantity'] or 0) - (current['options_exit_quantity'] or 0)
        if quantity <= 0 or current['accounting_status'] == 'incomplete':
            raise ValueError('Confirmed open quantity unavailable')
        oid = con.execute("INSERT INTO execution_orders (signal_id,broker,side,requested_quantity,status,requested_at,exit_reason,requester) "
                          "VALUES (?,?,'SELL',?,'SUBMITTING',?,?,?)",
                          (signal_id, trade['execution_broker'], quantity, _now(), reason, requester)).lastrowid
        con.execute("UPDATE signals SET status='exit_pending', execution_state='SUBMITTING', notes=? WHERE id=?", (notes, signal_id))
    return _submit(signal_id, oid, adapter, trade['options_symbol'], 'SELL', quantity)


def reconcile_trade(signal_id, broker=None):
    trade = dict(db.get_signal(signal_id) or {})
    if trade.get('mode') != 'live' or trade.get('status') == 'closed':
        return trade
    adapter = _broker(trade, broker)
    for order in db.execution_orders(signal_id):
        if not order['broker_order_id'] or order['status'] in TERMINAL:
            continue
        try:
            snapshot = adapter.get_order_execution(order['broker_order_id'])
            if snapshot.order_id != order['broker_order_id']:
                raise ValueError('Broker order identity mismatch')
            if (snapshot.side != order['side'] or snapshot.symbol != trade['options_symbol']
                    or snapshot.requested_quantity != order['requested_quantity']):
                raise ValueError('Broker order instrument, side or quantity mismatch')
            observation = None
            if snapshot.filled_quantity > order['filled_quantity'] or order['status'] == 'RECONCILING':
                try:
                    observation = adapter.get_underlying_observation(config.NIFTY_SYMBOL)
                    if not math.isfinite(observation.price) or observation.price <= 0 or not broker_time(observation.observed_at):
                        observation = None
                except Exception:
                    observation = None
            _record_snapshot(signal_id, order, snapshot, observation)
        except Exception as exc:
            logger.warning('Reconciliation pending for order %s: %s', order['broker_order_id'], exc)
            with db._conn() as con:
                con.execute('UPDATE execution_orders SET error=? WHERE id=?', (str(exc), order['id']))
    return dict(db.get_signal(signal_id))


def _record_snapshot(signal_id, order, snapshot, observation):
    just_closed = False
    with db._conn() as con:
        con.execute('BEGIN IMMEDIATE')
        current = con.execute('SELECT status FROM execution_orders WHERE id=?', (order['id'],)).fetchone()
        if current['status'] in TERMINAL:
            return
        if snapshot.filled_quantity < 0 or snapshot.filled_quantity > order['requested_quantity']:
            raise ValueError('Invalid broker filled quantity')
        for fill in snapshot.fills:
            if fill.quantity <= 0 or not math.isfinite(fill.price) or fill.price <= 0:
                raise ValueError('Invalid execution')
            prior = con.execute('SELECT quantity, premium FROM execution_fills WHERE order_id=? AND execution_id=?',
                                (order['id'], fill.execution_id)).fetchone()
            if prior and (prior['quantity'] != fill.quantity or prior['premium'] != fill.price):
                raise ValueError('Broker execution changed; manual reconciliation required')
            # A quote observed on recovery is not historical fill-time market data.
            underlying = observation
            stamp = broker_time(fill.timestamp)
            if underlying:
                observed_stamp = broker_time(underlying.observed_at)
                max_age = float(config.load_settings().get('ENTRY_QUOTE_MAX_AGE_SECONDS', config.ENTRY_QUOTE_MAX_AGE_SECONDS))
                if not stamp or not observed_stamp or abs((datetime.fromisoformat(observed_stamp) - datetime.fromisoformat(stamp)).total_seconds()) > max_age:
                    underlying = None
            con.execute("INSERT OR IGNORE INTO execution_fills "
                        "(order_id,broker,execution_id,quantity,premium,broker_timestamp,timestamp_basis,"
                        "underlying_price,underlying_observed_at,underlying_basis) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (order['id'], order['broker'], fill.execution_id, fill.quantity, fill.price,
                         broker_time(fill.timestamp), fill.timestamp_basis,
                         underlying.price if underlying else None, underlying.observed_at if underlying else None,
                         underlying.basis if underlying else None))
        total = con.execute('SELECT COALESCE(SUM(quantity),0) FROM execution_fills WHERE order_id=?', (order['id'],)).fetchone()[0]
        if total > order['requested_quantity']:
            raise ValueError('Executed quantity exceeds requested quantity')
        # History and trades endpoints can race. Re-poll instead of trusting an incomplete snapshot.
        consistent = total == snapshot.filled_quantity and (snapshot.status != 'COMPLETE' or total == order['requested_quantity'])
        state = snapshot.status if consistent else 'RECONCILING'
        con.execute('UPDATE execution_orders SET status=?, filled_quantity=?, confirmed_at=?, error=? WHERE id=?',
                    (state, snapshot.filled_quantity, _now(), None if consistent else 'Execution details incomplete', order['id']))
        bq, sq, complete = db.refresh_accounting(con, signal_id)
        row = dict(con.execute('SELECT * FROM signals WHERE id=?', (signal_id,)).fetchone())
        all_orders = list(con.execute('SELECT * FROM execution_orders WHERE signal_id=?', (signal_id,)))
        terminal = all(o['status'] in TERMINAL for o in all_orders)
        first = con.execute("SELECT f.* FROM execution_fills f JOIN execution_orders o ON o.id=f.order_id "
                            "WHERE o.signal_id=? AND o.side='BUY' ORDER BY f.broker_timestamp, f.id LIMIT 1", (signal_id,)).fetchone()
        if first:
            stamp = first['broker_timestamp']
            signal_stamp = broker_time(f"{row['date']}T{row['time_signal']}")
            delay = (datetime.fromisoformat(stamp) - datetime.fromisoformat(signal_stamp)).total_seconds() if stamp and signal_stamp else None
            con.execute("UPDATE signals SET option_fill_time=?, option_fill_time_basis=?, signal_to_fill_seconds=?, "
                        "underlying_at_option_fill=?, underlying_observed_at=?, underlying_fill_basis=? WHERE id=?",
                        (stamp, first['timestamp_basis'], delay, first['underlying_price'],
                         first['underlying_observed_at'], first['underlying_basis'], signal_id))
        if bq > 0 and bq == sq and complete and terminal:
            last = con.execute("SELECT f.* FROM execution_fills f JOIN execution_orders o ON o.id=f.order_id "
                               "WHERE o.signal_id=? AND o.side='SELL' ORDER BY f.broker_timestamp DESC, f.id DESC LIMIT 1", (signal_id,)).fetchone()
            last_order = [o for o in all_orders if o['side'] == 'SELL'][-1]
            price = last['underlying_price']
            points = ((price - row['entry']) * (1 if row['zone_class'] == 'demand' else -1)) if price is not None else None
            result = ('win' if points > 0 else 'loss' if points < 0 else 'breakeven') if points is not None else None
            just_closed = bool(con.execute("UPDATE signals SET status='closed', execution_state='CLOSED', "
                "option_exit_fill_time=?, exit_time=?, exit_price=?, exit_reason=?, closed_by=?, pnl_points=?, result=? "
                "WHERE id=? AND status!='closed'", (last['broker_timestamp'], last['broker_timestamp'], price,
                last_order['exit_reason'], last_order['requester'], round(points, 2) if points is not None else None,
                result, signal_id)).rowcount)
            if just_closed and points is not None:
                db._upsert_daily_summary(row['date'], points, result, con)
        else:
            if not complete or sq > bq:
                status = 'reconciliation_required'
            elif not bq and terminal:
                status = 'rejected'
            elif terminal:
                status = 'approved'
            elif any(o['side'] == 'SELL' for o in all_orders):
                status = 'exit_pending'
            else:
                status = 'entry_pending'
            con.execute('UPDATE signals SET status=?, execution_state=? WHERE id=?', (status, state, signal_id))
    if just_closed:
        try:
            import notify
            notify.execution_closed(dict(db.get_signal(signal_id)))
        except Exception:
            logger.exception('Trade closed but notification failed')


def reconcile_open_trades():
    for row in db.get_open_trades():
        if row['mode'] == 'live':
            try:
                trade = reconcile_trade(row['id'])
                if trade['status'] != 'closed' and trade.get('exit_reason'):
                    request_exit(row['id'], trade['exit_reason'], requester=trade.get('closed_by') or 'system')
            except Exception as exc:
                logger.warning('Trade %s needs reconciliation: %s', row['id'], exc)
