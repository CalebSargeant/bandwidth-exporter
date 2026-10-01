import pytest

from bandwidth_exporter import control
from bandwidth_exporter.control import KeyStore, NonceCache, signed_headers, verify
from bandwidth_exporter.dataplane import HELLO_SIZE, hello, parse_hello

KEY = b"0123456789abcdef0123456789abcdef"
NOW = 1_800_000_000.0


def signed(method="POST", path="/v1/slots", peer="node-a", body=b'{"x":1}', key=KEY, now=NOW):
    return signed_headers(key, method, path, peer, body, now=now)


def check(
    headers,
    method="POST",
    path="/v1/slots",
    body=b'{"x":1}',
    keys=None,
    nonces=None,
    allowed=(),
    now=NOW,
):
    return verify(
        headers,
        method,
        path,
        body,
        keys or KeyStore(shared=KEY),
        nonces or NonceCache(),
        allowed,
        now=now,
    )


def test_a_signed_request_verifies():
    verdict = check(signed())
    assert verdict.ok
    assert verdict.peer == "node-a"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"body": b'{"x":2}'}, "auth"),  # the requested bytes and duration are covered
        ({"path": "/v1/info"}, "auth"),
        ({"method": "DELETE"}, "auth"),
        ({"now": NOW + 61}, "auth"),  # outside the clock window
        ({"keys": KeyStore(shared=b"another-key-that-is-long-enough")}, "auth"),
        ({"allowed": ("node-b",)}, "forbidden"),
    ],
)
def test_tampering_is_refused(change, reason):
    verdict = check(signed(), **change)
    assert not verdict.ok
    assert verdict.reason == reason
    assert verdict.status == (403 if reason == "forbidden" else 401)


def test_missing_headers_are_refused():
    headers = signed()
    del headers[control.HEADER_SIGNATURE]
    assert check(headers).reason == "auth"


def test_replay_is_refused():
    nonces = NonceCache()
    headers = signed()
    assert check(headers, nonces=nonces).ok
    assert check(headers, nonces=nonces).reason == "replay"


def test_nonces_expire():
    nonces = NonceCache(ttl=10)
    assert nonces.check_and_add("n", 100.0)
    assert not nonces.check_and_add("n", 105.0)
    assert nonces.check_and_add("n", 111.0)


def test_per_peer_keys():
    keys = KeyStore.parse('{"node-a": "aaaaaaaaaaaaaaaaaaaa", "node-b": "bbbbbbbbbbbbbbbbbbbb"}')
    good = signed(key=b"aaaaaaaaaaaaaaaaaaaa")
    assert check(good, keys=keys).ok
    wrong = signed(key=b"bbbbbbbbbbbbbbbbbbbb")
    assert check(wrong, keys=keys).reason == "auth"
    stranger = signed(peer="node-z", key=b"aaaaaaaaaaaaaaaaaaaa")
    assert check(stranger, keys=keys).reason == "auth"


@pytest.mark.parametrize("value", ["", "short", '{"a": "short"}', "[]", "{}"])
def test_bad_key_material_is_refused(value):
    with pytest.raises(ValueError):
        KeyStore.parse(value)


def test_agent_key():
    assert control.agent_key(" " + KEY.decode() + "\n", "K") == KEY
    with pytest.raises(ValueError, match=r"\$K is empty"):
        control.agent_key(None, "K")
    with pytest.raises(ValueError, match="shorter"):
        control.agent_key("tiny", "K")


def test_hello_round_trip():
    token = bytes(range(16))
    packet = hello(token, 3, "upload")
    assert len(packet) == HELLO_SIZE
    assert parse_hello(packet) == (token, 3, "upload")
    assert parse_hello(b"XXXX" + packet[4:]) is None
    assert parse_hello(packet[:-1]) is None
