"""
Standalone Kite Connect login for the backtest workflow — no Streamlit needed.

    python -m backtest.login

Logging in at kite.zerodha.com (the website/app) does NOT create an API token.
Kite Connect has its own flow: open a login URL → authenticate → Kite redirects
to your app's redirect URI with ?request_token=... → exchange that (+ api_secret)
for an access_token, which is written to ./.kite_token and is valid until the
next trading day.

This does exactly what the dashboard's Engine tab does, from the terminal.
"""

from __future__ import annotations

import sys
from urllib.parse import parse_qs, urlparse

import config


def _extract_request_token(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if "request_token" in raw:
        q = parse_qs(urlparse(raw).query)
        return q.get("request_token", [""])[0]
    return raw            # user pasted just the token value


def main():
    if not config.KITE_API_KEY or not config.KITE_API_SECRET:
        sys.exit("KITE_API_KEY / KITE_API_SECRET missing in .env")

    from brokers.kite_adapter import KiteAdapter
    k = KiteAdapter()

    print("\n1. Open this URL, log in with your Kite password + TOTP:\n")
    print("   " + k.generate_login_url() + "\n")
    print("2. Kite redirects to your app's redirect URI. The page may show")
    print('   "site can\'t be reached" — that is fine. Copy the URL from the')
    print("   address bar (it contains ?request_token=...).\n")

    raw = input("Paste the redirect URL (or just the request_token): ")
    rt = _extract_request_token(raw)
    if not rt:
        sys.exit("Could not read a request_token from that input.")

    print("\nExchanging for access token ...")
    try:
        k.generate_session(rt)
    except Exception as e:
        sys.exit(f"{type(e).__name__}: {e}")

    print(f"✓ Saved to {config.TOKEN_FILE}")
    print("  Now run:  python -m backtest.data.fetch --check")


if __name__ == "__main__":
    main()
