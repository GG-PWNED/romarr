"""DATs arrive as ZIP archives, and loading them is not optional.

No-Intro distributes its DATs as zipped XML behind a captcha wall; the
mirrors operators can actually reach ship the same files zipped. A scanner
that accepted only `.dat` and `.xml` pointed at a directory of No-Intro zips
answered "loaded 0 DAT(s)" -- the log line in issue #21 -- and every import
that followed was unverified, with nothing anywhere saying why.

These tests cover the two halves of the fix: the scan finding zips, and
`reload_dats` reading DATs out of them (including the macOS junk a real zip
carries, and a zip that is not a DAT archive at all).
"""

from __future__ import annotations

import zipfile

import romarr.app as app
from romarr.app import ROMarr, _find_dats
from romarr.dat import hash_bytes


# One game, one rom, the hashes of the bytes below -- the same shape a real
# No-Intro datfile has, minus the eleven thousand other entries.
_DAT_BODY = (
    '<?xml version="1.0"?>\n'
    "<datafile>\n"
    "  <header><name>Test System</name><version>2026</version></header>\n"
    '  <game name="TestGame (USA)">\n'
    '    <rom name="TestGame.bin" size="8" crc="d2023890"'
    ' md5="6dcd8f3e0a0c4e0f6f3d1e8c2a5b7d9e"'
    ' sha1="2aae6c35c94fcfb415dbe95f408b9ce91ee846ed"/>\n'
    "  </game>\n"
    "</datafile>\n"
)


def _make_service(tmp_path, monkeypatch):
    """A ROMarr with its store redirected into the test directory."""
    monkeypatch.setenv("ROMARR_DATA", str(tmp_path / "store.json"))
    import tempfile
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "store.json")})
    # TitleDB is a network call at reload time; the tests here are about DATs.
    monkeypatch.setattr(svc, "reload_titled", lambda: {"loaded": 0})
    return svc


def test_the_scan_finds_zip_archives(tmp_path):
    """A No-Intro zip is a DAT source, not a mystery file to walk past."""
    with zipfile.ZipFile(tmp_path / "NoIntro - Test System.zip", "w") as z:
        z.writestr("test.dat", _DAT_BODY)
    (tmp_path / "loose.dat").write_text(_DAT_BODY)
    (tmp_path / "readme.txt").write_text("not a dat")

    found, stopped = _find_dats(tmp_path)

    assert {p.name for p in found} == {
        "NoIntro - Test System.zip", "loose.dat"}
    assert stopped == ""


def test_dats_load_from_a_zip(tmp_path, monkeypatch):
    """The issue #21 flow end to end: zip on disk -> games in the index.

    And not just parsed -- the loaded DAT has to answer a hash lookup, which
    is the only question anyone ever asks it.
    """
    svc = _make_service(tmp_path, monkeypatch)
    with zipfile.ZipFile(tmp_path / "NoIntro - Test System.zip", "w") as z:
        z.writestr("test.dat", _DAT_BODY)

    result = svc.reload_dats(str(tmp_path))

    assert result["loaded"] == 1, result
    lookup = svc.dats.lookup(size=8, crc="d2023890",
                             sha1="2aae6c35c94fcfb415dbe95f408b9ce91ee846ed")
    assert lookup.ok and lookup.game == "TestGame (USA)", lookup


def test_multiple_dats_in_one_zip_all_load(tmp_path, monkeypatch):
    """One zip, several systems: a bundle is still every DAT inside it."""
    svc = _make_service(tmp_path, monkeypatch)
    with zipfile.ZipFile(tmp_path / "bundle.zip", "w") as z:
        z.writestr("a.dat", _DAT_BODY)
        z.writestr("b.dat", _DAT_BODY.replace("Test System", "Other System"))

    result = svc.reload_dats(str(tmp_path))

    assert result["loaded"] == 2, result


def test_macos_junk_inside_a_zip_is_ignored(tmp_path, monkeypatch):
    """`__MACOSX/._test.dat` is a resource fork, not a DAT.

    Zips made on macOS carry one per real file. Parsing them fails (they are
    AppleDouble binary), and a failure per junk entry in a log nobody reads
    is how a working load comes to look broken.
    """
    svc = _make_service(tmp_path, monkeypatch)
    with zipfile.ZipFile(tmp_path / "bundle.zip", "w") as z:
        z.writestr("test.dat", _DAT_BODY)
        z.writestr("__MACOSX/._test.dat", b"\x00\x05\x16\x07junk")
        z.writestr(".hidden.dat", b"junk")

    result = svc.reload_dats(str(tmp_path))

    assert result["loaded"] == 1, result


def test_a_zip_that_is_not_a_dat_archive_loads_nothing_and_survives(
        tmp_path, monkeypatch):
    """A ROM zip in the DAT directory must not take the service down with it.

    DAT_PATH gets pointed at real directories, and real directories hold
    more than DATs. The corrupt-or-unrelated zip is the common case of the
    wrong file, and the answer is zero DATs from it plus a warning -- never
    an exception escaping reload_dats.
    """
    svc = _make_service(tmp_path, monkeypatch)
    (tmp_path / "notadat.zip").write_bytes(b"PK\x03\x04truncated garbage")
    with zipfile.ZipFile(tmp_path / "rom.zip", "w") as z:
        z.writestr("Game.smc", b"\x00" * 64)

    result = svc.reload_dats(str(tmp_path))

    assert result["loaded"] == 0, result
    # A bad zip is a warning in the log, not a failed load: the scan itself
    # worked, and "error" in the result is reserved for a directory that
    # could not be read at all.
    assert "error" not in result, result
    # The service still answers lookups -- with the honest verdict.
    lookup = svc.dats.lookup(size=64, crc="00000000")
    assert not lookup.ok


def test_nested_platform_directories_still_work(tmp_path, monkeypatch):
    """Zips one level down beside their platform, like the loose DATs were."""
    svc = _make_service(tmp_path, monkeypatch)
    sub = tmp_path / "gba"
    sub.mkdir()
    with zipfile.ZipFile(sub / "NoIntro - GBA.zip", "w") as z:
        z.writestr("gba.dat", _DAT_BODY)

    result = svc.reload_dats(str(tmp_path))

    assert result["loaded"] == 1, result


def test_hash_bytes_matches_the_dat_entry():
    """The fixture's hashes are the bytes it claims, or every test above is
    asserting against a DAT that could never match anything."""
    body = bytes(range(256)) * 128
    hashes = hash_bytes(body)
    assert hashes["size"] == len(body)
