import config


def calculate(entry: float, stop_loss: float, trades_today: int = 0) -> dict:
    """
    S.E.T.S — Stop → Entry → Target → Size

    Risk per trade = Max daily loss ÷ remaining trade slots.
    Position size  = Risk per trade ÷ (Entry − SL) in points.

    Returns a dict with all sizing details for logging.
    """
    remaining_trades = config.MAX_TRADES_PER_DAY - trades_today
    if remaining_trades <= 0:
        return {"error": "Max trades per day reached", "position_size": 0}

    risk_per_trade = config.MAX_DAILY_LOSS / remaining_trades
    points_at_risk = abs(entry - stop_loss)

    if points_at_risk == 0:
        return {"error": "Entry == Stop Loss", "position_size": 0}

    position_size = risk_per_trade / points_at_risk

    return {
        "capital":           config.CAPITAL,
        "max_daily_loss":    config.MAX_DAILY_LOSS,
        "trades_today":      trades_today,
        "remaining_trades":  remaining_trades,
        "risk_per_trade":    round(risk_per_trade, 2),
        "entry":             entry,
        "stop_loss":         stop_loss,
        "points_at_risk":    round(points_at_risk, 2),
        "position_size":     round(position_size, 4),   # lots / units
        "error":             None,
    }
