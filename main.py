"""
Nifty Options Trading Engine — Entry Point

Run:  python main.py          → interactive menu
      python main.py --token  → generate today's Kite token (for scripts/VPS)
      python main.py --run    → start engine directly (for scripts/VPS)
      python main.py --scan   → one-time test scan, bypass market hours
      python main.py --status → print today's P&L and exit
      python main.py --export → export today's CSV and exit
"""

import argparse
import sys


# ── Menu ──────────────────────────────────────────────────────────────────

def show_menu():
    import config
    from journal.db import init_db, trades_today, daily_pnl
    init_db()

    while True:
        print()
        print("=" * 48)
        print(f"   NIFTY TRADING SYSTEM  |  mode: {config.MODE.upper()}")
        print("=" * 48)
        print(f"   Today  →  trades: {trades_today()}  |  P&L: {daily_pnl():.2f} pts")
        print("-" * 48)
        print("   1.  Generate today's Kite token")
        print("   2.  Start engine  (waits for 10:05 AM)")
        print("   3.  Scan now      (test — bypass time check)")
        print("   4.  Today's status")
        print("   5.  Export today's CSV")
        print("   6.  Exit")
        print("=" * 48)

        choice = input("   Choose [1-6]: ").strip()

        if choice == "1":
            cmd_token()
        elif choice == "2":
            cmd_run()
        elif choice == "3":
            cmd_scan_now()
        elif choice == "4":
            cmd_status()
        elif choice == "5":
            cmd_export()
        elif choice == "6":
            print("   Bye.")
            sys.exit(0)
        else:
            print("   Invalid choice. Enter 1–6.")


# ── Commands ──────────────────────────────────────────────────────────────

def cmd_token():
    import config
    from brokers.kite_adapter import KiteAdapter
    k = KiteAdapter()

    if config.KITE_TOKEN_MODE == "auto":
        print("\n   Open this URL in your browser (phone or laptop):")
        print(f"   {k.generate_login_url()}")
        print(f"\n   Waiting for Kite redirect on port {config.KITE_TOKEN_PORT}...")
        k.capture_token_via_server(port=config.KITE_TOKEN_PORT)
        print("   Token captured and saved automatically.")
    else:
        print("\n   Step 1 — Open this URL in your browser:")
        print(f"   {k.generate_login_url()}")
        print("\n   Step 2 — After login, copy the request_token from the redirect URL.")
        print("   Redirect looks like: http://127.0.0.1/?request_token=XXXXXX&status=success")
        request_token = input("\n   Paste request_token here: ").strip()
        if not request_token:
            print("   No token entered.")
            return
        k.generate_session(request_token)
        print("   Token saved. Engine is ready to run.")


def cmd_run():
    from scheduler import run
    run()


def cmd_scan_now():
    from scheduler import scan_now
    scan_now()
    print("\n   Scan complete. Check above for any signals found.")


def cmd_status():
    from journal.db import init_db, trades_today, daily_pnl
    init_db()
    print(f"\n   Trades today : {trades_today()}")
    print(f"   Daily P&L    : {daily_pnl():.2f} pts")


def cmd_export():
    from journal.db import init_db
    from journal.export import export_day
    init_db()
    path = export_day()
    print(f"\n   Exported → {path}")


# ── Entrypoint ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--token",  action="store_true")
    parser.add_argument("--run",    action="store_true")
    parser.add_argument("--scan",   action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()

    if   args.token:  cmd_token()
    elif args.run:    cmd_run()
    elif args.scan:   cmd_scan_now()
    elif args.status: cmd_status()
    elif args.export: cmd_export()
    else:             show_menu()   # default — interactive menu


if __name__ == "__main__":
    main()
