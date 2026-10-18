"""The gate's `next` contract: always a local /app/ path, never scheme/host."""

from urllib.parse import parse_qs, urlsplit


def test_gate_next_is_safe_local_path(client):
    response = client.get("/app/?x=1")
    assert response.status_code == 302
    parts = urlsplit(response["Location"])
    assert (parts.scheme, parts.netloc, parts.path) == ("", "", "/auth.html")
    (nxt,) = parse_qs(parts.query)["next"]
    assert nxt.startswith("/app/")
    assert not nxt.startswith("//")
    nxt_parts = urlsplit(nxt)
    assert (nxt_parts.scheme, nxt_parts.netloc) == ("", "")
