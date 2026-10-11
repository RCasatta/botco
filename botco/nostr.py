"""Posting to Nostr: signed notes sent to a few relays.

Posting costs nothing, so published posts are mirrored here from the X
publisher. A note is a kind 1 event signed with the account's key (BIP-340
Schnorr over secp256k1); any relay that accepts it serves it.

    python -m botco.nostr keygen nostr.env
    python -m botco.nostr profile nostr.env --name "Local AI bot" --about "..."

keygen writes a new key to nostr.env (never printed) and shows the public
key; profile publishes the account's name and description, flagged as a bot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from coincurve import PrivateKey, PublicKeyXOnly
from websockets.sync.client import connect

from .xclient import read_env

log = logging.getLogger(__name__)

KEY = "NOSTR_SECRET_KEY"
RELAYS = ["wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net", "wss://relay.nostr.band"]

# bech32 (BIP-173), for nsec/npub/note strings.
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert(data: bytes | list[int], frm: int, to: int, pad: bool) -> list[int]:
    acc, bits, out, maxv = 0, 0, [], (1 << to) - 1
    for v in data:
        acc = (acc << frm) | v
        bits += frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (to - bits)) & maxv)
    elif not pad and (bits >= frm or (acc << (to - bits)) & maxv):
        raise ValueError("invalid bech32 padding")
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    five = _convert(data, 8, 5, True)
    poly = _polymod(_hrp_expand(hrp) + five + [0] * 6) ^ 1
    return hrp + "1" + "".join(CHARSET[d] for d in five + [(poly >> 5 * (5 - i)) & 31 for i in range(6)])


def bech32_decode(text: str) -> tuple[str, bytes]:
    text = text.strip().lower()
    hrp, _, rest = text.rpartition("1")
    data = [CHARSET.index(c) for c in rest]
    if not hrp or len(data) < 6 or _polymod(_hrp_expand(hrp) + data) != 1:
        raise ValueError("invalid bech32 string")
    return hrp, bytes(_convert(data[:-6], 5, 8, False))


def secret_key(value: str) -> PrivateKey:
    """From nsec1... or 64 hex characters."""
    value = value.strip()
    if value.startswith("nsec1"):
        hrp, raw = bech32_decode(value)
        if hrp != "nsec":
            raise ValueError("not an nsec key")
        return PrivateKey(raw)
    return PrivateKey(bytes.fromhex(value))


def public_hex(key: PrivateKey) -> str:
    return key.public_key.format(compressed=True)[1:].hex()


def sign_event(key: PrivateKey, kind: int, content: str, tags: list[list[str]], created_at: int | None = None) -> dict:
    """A NIP-01 event, with its id and signature."""
    pubkey = public_hex(key)
    created_at = int(time.time()) if created_at is None else created_at
    serial = json.dumps([0, pubkey, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    eid = hashlib.sha256(serial.encode()).digest()
    return {"id": eid.hex(), "pubkey": pubkey, "created_at": created_at, "kind": kind, "tags": tags,
            "content": content, "sig": key.sign_schnorr(eid).hex()}


def verify(event: dict) -> bool:
    serial = json.dumps([0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
                        separators=(",", ":"), ensure_ascii=False)
    eid = hashlib.sha256(serial.encode()).digest()
    return eid.hex() == event["id"] and \
        PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(bytes.fromhex(event["sig"]), eid)


def send(event: dict, relays: list[str], timeout: float = 10) -> dict[str, str]:
    """Send an event to each relay: relay -> "ok" or why not."""
    out = {}
    for url in relays:
        try:
            with connect(url, open_timeout=timeout, close_timeout=2) as ws:
                ws.send(json.dumps(["EVENT", event]))
                deadline = time.monotonic() + timeout
                while True:
                    msg = json.loads(ws.recv(timeout=max(deadline - time.monotonic(), 0.1)))
                    if msg[0] == "OK" and msg[1] == event["id"]:
                        out[url] = "ok" if msg[2] else f"refused: {msg[3] if len(msg) > 3 else ''}"
                        break
        except Exception as e:  # noqa: BLE001 - one relay down is normal
            out[url] = f"{type(e).__name__}: {e}"[:200]
    return out


@dataclass
class Posted:
    id: str
    accepted: list[str]
    refused: dict[str, str]

    @property
    def link(self) -> str:
        return f"https://njump.me/{bech32_encode('note', bytes.fromhex(self.id))}"


class Nostr:
    def __init__(self, env: dict[str, str], relays: list[str], tags: list[str]):
        if not env.get(KEY):
            raise ValueError(f"the Nostr env file is missing {KEY}")
        self.key = secret_key(env[KEY])
        self.relays = relays
        self.tags = tags

    def npub(self) -> str:
        return bech32_encode("npub", bytes.fromhex(public_hex(self.key)))

    def post(self, text: str) -> Posted:
        """A note with the configured hashtags appended; raises if no relay
        took it."""
        content = text + ("\n\n" + " ".join(f"#{t}" for t in self.tags) if self.tags else "")
        event = sign_event(self.key, 1, content, [["t", t.lower()] for t in self.tags])
        result = send(event, self.relays)
        accepted = [r for r, v in result.items() if v == "ok"]
        if not accepted:
            raise RuntimeError("no relay accepted the note: " + "; ".join(f"{r}: {v}" for r, v in result.items()))
        return Posted(event["id"], accepted, {r: v for r, v in result.items() if v != "ok"})


class DryRunNostr:
    def post(self, text: str) -> Posted:
        return Posted("0" * 64, [], {})


def make(cfg) -> Nostr | DryRunNostr | None:
    """From [sinks.nostr]; None when it is not configured."""
    if cfg is None:
        return None
    if cfg.dry_run:
        return DryRunNostr()
    return Nostr(read_env(cfg.env_file), cfg.relays, cfg.tags)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="write a new key to the env file")
    k.add_argument("env_file", type=Path)
    p = sub.add_parser("profile", help="publish the account's name and description")
    p.add_argument("env_file", type=Path)
    p.add_argument("--name", required=True)
    p.add_argument("--about", required=True)
    p.add_argument("--picture", default="")
    p.add_argument("--website", default="")
    p.add_argument("--relay", action="append", help="instead of the default relays")
    args = ap.parse_args()
    if args.cmd == "keygen":
        if args.env_file.exists() and KEY in read_env(args.env_file):
            sys.exit(f"{args.env_file} already has a key")
        key = PrivateKey()
        with args.env_file.open("a") as f:
            f.write(f"{KEY}={bech32_encode('nsec', key.secret)}\n")
        args.env_file.chmod(0o600)
        print(bech32_encode("npub", bytes.fromhex(public_hex(key))))
        return
    nostr = Nostr(read_env(args.env_file), args.relay or RELAYS, [])
    meta = {"name": args.name, "display_name": args.name, "about": args.about, "bot": True}
    if args.picture:
        meta["picture"] = args.picture
    if args.website:
        meta["website"] = args.website
    event = sign_event(nostr.key, 0, json.dumps(meta, ensure_ascii=False), [])
    for relay, result in send(event, nostr.relays).items():
        print(f"{relay}: {result}")
    print(nostr.npub())


if __name__ == "__main__":
    main()
