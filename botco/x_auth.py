"""Authorize the developer app to post as the bot account (OAuth 1.0a PIN flow).

    python -m botco.x_auth x.env --expect Local_AI_bot

x.env must already hold X_CONSUMER_KEY and X_CONSUMER_SECRET (the app's keys).
The script prints a link: open it while logged in to X as the bot, authorize
the app, and type the PIN X shows. It then adds X_ACCESS_TOKEN and
X_ACCESS_TOKEN_SECRET to x.env, but only if the account that authorized is the
expected one: a token for the wrong account would post as that account.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from requests_oauthlib import OAuth1Session

from .xclient import read_env

OAUTH = "https://api.x.com/oauth"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("env_file", type=Path)
    ap.add_argument("--expect", required=True, help="the bot's handle, without @")
    args = ap.parse_args()

    env = read_env(args.env_file)
    key, secret = env.get("X_CONSUMER_KEY"), env.get("X_CONSUMER_SECRET")
    if not key or not secret:
        sys.exit(f"{args.env_file} needs X_CONSUMER_KEY and X_CONSUMER_SECRET first")

    session = OAuth1Session(key, client_secret=secret, callback_uri="oob")
    request = session.fetch_request_token(f"{OAUTH}/request_token")
    # The classic page signs in on the page itself; api.x.com hands sign-in to
    # x.com's new login flow, which can lose the authorize step.
    print("In a private window logged in to X as the bot account, open:\n")
    print(f"  {session.authorization_url('https://api.twitter.com/oauth/authorize')}\n")
    print(f"or, if that page fails:\n\n  {session.authorization_url(f'{OAUTH}/authorize')}\n")
    pin = input("PIN shown by X: ").strip()

    session = OAuth1Session(key, client_secret=secret, resource_owner_key=request["oauth_token"],
                            resource_owner_secret=request["oauth_token_secret"], verifier=pin)
    tokens = session.fetch_access_token(f"{OAUTH}/access_token")
    who = tokens.get("screen_name", "")
    if who.lower() != args.expect.lower().lstrip("@"):
        sys.exit(f"Authorized as @{who}, not @{args.expect}: nothing saved. "
                 f"Log out, log in as @{args.expect}, and run this again.")

    lines = [line for line in args.env_file.read_text().splitlines()
             if not line.startswith(("X_ACCESS_TOKEN=", "X_ACCESS_TOKEN_SECRET="))]
    lines += [f"X_ACCESS_TOKEN={tokens['oauth_token']}", f"X_ACCESS_TOKEN_SECRET={tokens['oauth_token_secret']}"]
    args.env_file.write_text("\n".join(lines) + "\n")
    args.env_file.chmod(0o600)
    print(f"Saved the tokens for @{who} in {args.env_file}.")


if __name__ == "__main__":
    main()
