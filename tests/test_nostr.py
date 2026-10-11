import json
import threading

from coincurve import PrivateKey
from websockets.sync.server import serve

from botco.nostr import Nostr, bech32_decode, bech32_encode, secret_key, send, sign_event, verify


def test_bech32_matches_nip19():
    pub = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
    npub = "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg"
    assert bech32_encode("npub", bytes.fromhex(pub)) == npub
    assert bech32_decode(npub) == ("npub", bytes.fromhex(pub))
    nsec = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
    assert secret_key(nsec).secret.hex() == "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"


def test_events_are_signed_and_verifiable():
    key = PrivateKey()
    event = sign_event(key, 1, "Six-bit KV cache: half the VRAM ✓", [["t", "localai"]], created_at=1791600000)
    assert verify(event)
    assert not verify({**event, "content": "changed"})


def relay(accept: bool):
    """A one-shot fake relay on localhost; returns its URL and what it got."""
    got = []

    def handler(ws):
        msg = json.loads(ws.recv())
        got.append(msg)
        ws.send(json.dumps(["OK", msg[1]["id"], accept, "" if accept else "blocked: spam"]))

    server = serve(handler, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"ws://127.0.0.1:{server.socket.getsockname()[1]}", got, server


def test_a_note_goes_to_the_relays_that_take_it():
    ok, got, s1 = relay(True)
    no, _, s2 = relay(False)
    key = PrivateKey()
    nostr = Nostr({"NOSTR_SECRET_KEY": key.secret.hex()}, [ok, no, "ws://127.0.0.1:9"], ["localai"])
    posted = nostr.post("A 27B model at 4 bits fits two 16 GB cards.")
    assert posted.accepted == [ok] and "spam" in posted.refused[no] and "ws://127.0.0.1:9" in posted.refused
    (kind, event), = got
    assert kind == "EVENT" and verify(event) and event["tags"] == [["t", "localai"]]
    assert event["content"].endswith("\n\n#localai") and posted.link.startswith("https://njump.me/note1")
    assert send(event, [no])[no].startswith("refused")
    s1.shutdown()
    s2.shutdown()
