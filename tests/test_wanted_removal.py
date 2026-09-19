"""Dropping a request you no longer want.

Wanted could only ever be emptied by an import. A typo -- "Super Metrod" --
stayed on the list and kept costing an indexer search on every missing sweep
and every RSS pass, for a game that does not exist, and the only cure was
editing romarr.json and restarting the container.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from romarr.app import ROMarr, make_handler
from romarr.store import Store


# --- the store ------------------------------------------------------------

def test_unwant_drops_the_entry(tmp_path):
    s = Store(tmp_path / "r.json")
    s.want("Super Metrod", "snes")
    s.want("Contra", "nes")

    assert s.unwant("Super Metrod", "snes") is True
    assert [w.game for w in s.wanted] == ["Contra"]


def test_unwant_matches_case_the_way_want_does(tmp_path):
    """`want` treats a second request as a retry regardless of case, so the
    removal has to agree, or a title added as "contra" is undroppable as
    "Contra"."""
    s = Store(tmp_path / "r.json")
    s.want("Contra", "nes")
    assert s.unwant("CONTRA", "nes") is True
    assert s.wanted == []


def test_unwant_leaves_the_same_title_on_another_platform(tmp_path):
    s = Store(tmp_path / "r.json")
    s.want("Contra", "nes")
    s.want("Contra", "snes")
    s.unwant("Contra", "nes")
    assert [(w.game, w.platform) for w in s.wanted] == [("Contra", "snes")]


def test_unwant_says_when_there_was_nothing_to_drop(tmp_path):
    s = Store(tmp_path / "r.json")
    assert s.unwant("Never Requested", "nes") is False


def test_a_dropped_request_stays_dropped_across_a_restart(tmp_path):
    """The bug this fixes was that editing the file was the only way; a
    removal that lived in memory would be exactly as useless."""
    path = tmp_path / "r.json"
    s = Store(path)
    s.want("Super Metrod", "snes")
    s.unwant("Super Metrod", "snes")

    assert Store(path).wanted == []


# --- the service ----------------------------------------------------------

def svc(tmp_path, **env):
    base = {"ROMARR_DATA": str(tmp_path / "s.json")}
    base.update(env)
    return ROMarr(base)


def test_dropping_a_request_is_recorded_as_its_own_kind_of_event(tmp_path):
    """An import and a change of mind both empty a row, and History has to be
    able to tell them apart afterwards."""
    service = svc(tmp_path)
    service.store.want("Super Metrod", "snes")

    assert service.drop_request("Super Metrod", "snes") is True
    kinds = [e["kind"] for e in service.store.history()]
    assert "ignored" in kinds
    assert service.store.missing() == []


def test_dropping_accepts_a_platform_name_as_well_as_a_slug(tmp_path):
    service = svc(tmp_path)
    service.store.want("Contra", "snes")
    assert service.drop_request("Contra", "Super Nintendo") is True
    assert service.store.missing() == []


def test_dropping_something_that_is_not_wanted_records_nothing(tmp_path):
    service = svc(tmp_path)
    assert service.drop_request("Nothing", "nes") is False
    assert service.store.history() == []


# --- over the wire --------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                      "ROMARR_API_KEY": "testkey"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", service
    httpd.shutdown()
    httpd.server_close()


def call(url, method="GET", body=None, key="testkey"):
    request = urllib.request.Request(url, method=method)
    if key:
        request.add_header("X-Api-Key", key)
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_delete_wanted_missing_removes_the_row(server):
    base, service = server
    service.store.want("Super Metrod", "snes")

    status, payload = call(
        f"{base}/api/v1/wanted/missing?game=Super%20Metrod&platform=snes",
        method="DELETE")

    assert status == 200 and payload["deleted"] is True
    assert service.store.missing() == []


def test_delete_wanted_missing_takes_a_json_body_too(server):
    base, service = server
    service.store.want("Contra", "nes")

    status, payload = call(f"{base}/api/v1/wanted/missing", method="DELETE",
                           body={"game": "Contra", "platform": "nes"})

    assert status == 200 and payload["deleted"] is True
    assert service.store.missing() == []


def test_delete_wanted_missing_needs_both_parts(server):
    base, _ = server
    status, payload = call(f"{base}/api/v1/wanted/missing?game=Contra",
                           method="DELETE")
    assert status == 400 and "required" in payload["error"]


def test_deleting_something_not_wanted_is_a_404(server):
    base, _ = server
    status, payload = call(
        f"{base}/api/v1/wanted/missing?game=Ghosts&platform=nes",
        method="DELETE")
    assert status == 404 and payload["deleted"] is False


def test_dropping_a_request_still_needs_the_key(server):
    base, service = server
    service.store.want("Contra", "nes")
    status, _ = call(f"{base}/api/v1/wanted/missing?game=Contra&platform=nes",
                     method="DELETE", key=None)
    assert status in (401, 403)
    assert len(service.store.missing()) == 1
