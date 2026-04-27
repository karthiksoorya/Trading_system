"""
Entry point.

Usage:
  python main.py            → start the trading engine (paper mode by default)
  python main.py --token    → generate today's Kite access token
  python main.py --export   → export today's trades to CSV and exit
  python main.py --status   → print today's trade count and P&L and exit
"""

import argparse
import sys


def cmd_token():
    from brokers.kite_adapter import KiteAdapter
    k = KiteAdapter()
    print("\nStep 1 — Open this URL in your browser:")
    print(k.generate_login_url())
    print("\nStep 2 — After login, copy the 'request_token' from the redirect URL.")
    request_token = input("Paste request_token here: ").strip()
    if not request_token:
        print("No token entered. Exiting.")
        sys.exit(1)
    k.generate_session(request_token)
    print("Access token saved. You can now run: python main.py")


def cmd_export():
    from journal.db import init_db
    from journal.export import export_day
    init_db()
    path = export_day()
    print(f"Exported → {path}")


def cmd_status():
    from journal.db import init_db, trades_today, daily_pnl
    init_db()
    print(f"Trades today : {trades_today()}")
    print(f"Daily P&L    : {daily_pnl():.2f} pts")


def cmd_run():
    from scheduler import run
    run()


def main():
    parser = argparse.ArgumentParser(description="Nifty Options Trading Engine")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--token",  action="store_true", help="Generate today's Kite token")
    group.add_argument("--export", action="store_true", help="Export today's CSV and exit")
    group.add_argument("--status", action="store_true", help="Print today's P&L and exit")
    args = parser.parse_args()

    if args.token:   cmd_token()
    elif args.export: cmd_export()
    elif args.status: cmd_status()
    else:            cmd_run()


if __name__ == "__main__":
    main()
