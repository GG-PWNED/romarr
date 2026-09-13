"""Batch decompression of a compressed ROM library.

The workflow issue #22 asked for: a library that was compressed (with
compressatorium or anything else) can be decompressed, every output validated
against the loaded DATs, and the compressed originals deleted -- but only the
ones that passed validation.

Division of labour:

  * **Compressatorium** does the extraction. It speaks CHD, RVZ/WIA/GCZ,
    Z3DS, NSZ, CSO/ZSO/DAX, WUX, NKit and plain 7z/zip, each through the
    right tool (chdman, dolphin-tool, maxcso, nsz...). ROMarr does not
    reimplement any of that; it submits jobs to a running service.
  * **ROMarr** does the verdict. The DAT index lives here, and "is this the
    published dump" is answered by hash against that index -- the same
    question `dat.lookup` answers on every import. The service's own
    `/api/dat/match` is consulted as a second opinion for the formats whose
    hashes are embedded in the container (CHD, Dolphin images), where a
    plain file hash of the container cannot match a DAT of its contents.

Safety model, because the last step deletes files:

  * Originals are deleted only when `delete_originals=True` is passed
    explicitly AND every output of that original matched a DAT entry.
  * An output that is `unknown` -- no DAT claims it -- keeps its original.
    No DAT does not mean bad file: homebrew, translations and anything an
    out-of-date DAT has not seen yet all land there, and deleting on
    unknown would destroy exactly the files a DAT cannot protect.
  * A `bad-dump` verdict keeps the original and fails the item loudly.
  * With no DATs loaded at all, nothing is ever deleted, however the flags
    are set. Verification is the gate; without it the gate stays shut.
  * Every step is logged: scan, submit, completion, per-file verdict, and
    each deletion. The log is the audit trail for a 500-file batch.

Dry run is a first-class mode: it scans and reports what would be processed
without submitting a single job, so an operator can see the blast radius
before authorising a destructive one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compressatorium import (
    CANCELLED, COMPLETED, FAILED, CompressatoriumClient,
    CompressatoriumConfig, JobResult, mode_for,
)
from .dat import BAD_DUMP, UNKNOWN, VERIFIED, DatIndex, hash_file

log = logging.getLogger(__name__)


# Extensions that count as "compressed" for the purposes of batch
# decompression. Kept in sync with compressatorium.mode_for: anything the
# client cannot name a mode for is not scanned, so the two lists agreeing is
# what makes the scan honest about what it can actually do.
COMPRESSED_EXTENSIONS: frozenset[str] = frozenset({
    # General-purpose archives
    ".7z", ".zip",
    # Compressed disc images (CHD, Dolphin RVZ/WIA/GCZ, CSO family)
    ".chd", ".rvz", ".wia", ".gcz", ".cso", ".zso", ".dax",
    # Compressed 3DS ROMs -- the z-prefixed forms only. `.cci`/`.cxi`/
    # `.cia` are the decompressed originals and must never be treated as
    # work to do.
    ".z3ds", ".zcxi", ".zcia", ".zcci", ".z3dsx",
    # Compressed Switch ROMs
    ".nsz", ".xcz",
    # Compressed Wii U images
    ".wux",
    # NKit-shrunk GameCube/Wii images (compound extensions -- matched with
    # endswith, never with Path.suffix)
    ".nkit.iso", ".nkit.gcz",
})

# How long to wait for the whole batch, and how often to poll. A CHD of a
# dual-layer DVD decompresses in minutes on spinning rust; a hundred of them
# queue behind one another on the service, so the batch budget is generous
# and the poll interval short enough that small jobs are not held up.
DEFAULT_JOB_TIMEOUT = 3 * 3600
DEFAULT_POLL_INTERVAL = 5


@dataclass
class DecompressItem:
    """One source file in the batch and what happened to it."""

    source: str
    status: str = "pending"
    # pending | skipped | submitted | decompressed | verified | failed |
    # deleted | kept
    job_id: str = ""
    mode: str = ""
    output_files: list[str] = field(default_factory=list)
    dat_match: str = ""          # verified | bad-dump | unknown | ""
    dat_detail: str = ""
    error: str = ""
    deleted: bool = False


@dataclass
class DecompressReport:
    """Summary of a batch run."""

    scanned: int = 0
    submitted: int = 0
    decompressed: int = 0
    verified: int = 0
    failed: int = 0
    deleted: int = 0
    kept: int = 0
    skipped: int = 0
    items: list[DecompressItem] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "submitted": self.submitted,
            "decompressed": self.decompressed,
            "verified": self.verified,
            "failed": self.failed,
            "deleted": self.deleted,
            "kept": self.kept,
            "skipped": self.skipped,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "error": self.error,
            "items": [
                {
                    "source": it.source,
                    "status": it.status,
                    "job_id": it.job_id,
                    "mode": it.mode,
                    "output_files": it.output_files,
                    "dat_match": it.dat_match,
                    "dat_detail": it.dat_detail,
                    "error": it.error,
                    "deleted": it.deleted,
                }
                for it in self.items
            ],
        }


def scan_compressed(directory: str | Path, *,
                    extensions: frozenset[str] | None = None) -> list[Path]:
    """Walk `directory` and return every file ROMarr knows how to decompress.

    Recursive, but only files whose extension has a decompression mode are
    returned -- a directory walk that pulled in `.iso` or `.n64` would queue
    jobs the service cannot do anything with.
    """
    exts = extensions if extensions is not None else COMPRESSED_EXTENSIONS
    root = Path(directory)
    if not root.is_dir():
        log.warning("decompress: %s is not a directory", root)
        return []
    found: list[Path] = []
    for entry in root.rglob("*"):
        if not entry.is_file():
            continue
        name = entry.name.lower()
        # Endswith rather than suffix: NKit images carry compound extensions
        # (`game.nkit.iso`) that `Path.suffix` reduces to `.iso`, and `.iso`
        # is NOT compressed -- matching on the suffix alone would either miss
        # every NKit file or, worse, pull in plain disc images.
        if any(name.endswith(ext) for ext in exts):
            # And the client must actually know a mode for it, so the scan
            # and the job submission can never disagree.
            if mode_for(name):
                found.append(entry)
    found.sort(key=lambda p: p.name.lower())
    return found


def _output_files(output_path: str) -> list[str]:
    """Every regular file a job produced.

    `output_path` is a file for disc-image conversions (CHD -> cue/bin/iso)
    and a directory for archive extractions (7z -> its contents), so both
    shapes are walked the same way.
    """
    if not output_path:
        return []
    out = Path(output_path)
    if out.is_file():
        return [str(out)]
    if out.is_dir():
        return sorted(str(p) for p in out.rglob("*") if p.is_file())
    return []


def _verify_outputs(files: list[str], dats: DatIndex,
                    client: CompressatoriumClient) -> tuple[str, str]:
    """Check every decompressed file against the DATs.

    Returns (status, detail) where status is "verified", "bad-dump" or
    "unknown". Verified means EVERY output matched a DAT entry. The service's
    own /api/dat/match is consulted as a second opinion for each file, which
    is what catches CHD and Dolphin outputs whose hashes are embedded in the
    container rather than being a plain file hash.
    """
    if not files:
        return UNKNOWN, "no output files produced"
    worst = VERIFIED
    details: list[str] = []
    for fpath in files:
        p = Path(fpath)
        if not p.is_file():
            worst = BAD_DUMP
            details.append(f"output missing: {fpath}")
            continue
        matched = False
        detail = ""
        file_bad = False
        # ROMarr's own index first -- it is local and free.
        try:
            hashes = hash_file(p)
            match = dats.lookup(**hashes)
            if match.status == VERIFIED:
                matched = True
                detail = f"{match.game} ({match.dat})"
            elif match.status == BAD_DUMP:
                file_bad = True
                detail = match.detail or "bad dump"
        except OSError as err:
            worst = BAD_DUMP
            details.append(f"could not hash {p.name}: {err}")
            continue
        if not matched:
            # Second opinion: the service's embedded-hash matching answers
            # for CHD/Dolphin containers what a file hash cannot.
            remote = client.match_file(fpath)
            if remote.matched:
                matched = True
                detail = f"{remote.game or 'matched'} ({remote.dat or 'server DAT'})"
            elif remote.detail:
                detail = detail or remote.detail
        if matched:
            details.append(f"{p.name}: verified {detail}".rstrip())
        elif file_bad:
            worst = BAD_DUMP
            details.append(f"{p.name}: {detail}")
        else:
            if worst == VERIFIED:
                worst = UNKNOWN
            details.append(f"{p.name}: {detail or 'not in any loaded DAT'}")
    # A bad-dump verdict outranks unknown: one corrupt output means the
    # extraction is suspect even if its siblings verified.
    return worst, "; ".join(details)


def run_batch(
    directory: str | Path,
    *,
    compressatorium_url: str = "",
    compressatorium_api_key: str = "",
    dats: DatIndex | None = None,
    delete_originals: bool = False,
    output_dir: str | Path | None = None,
    job_timeout: int = DEFAULT_JOB_TIMEOUT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    dry_run: bool = False,
) -> DecompressReport:
    """Decompress every compressed file in `directory`.

    Parameters
    ----------
    directory
        Root to scan for compressed files.
    compressatorium_url
        Base URL of the service. Required unless dry_run.
    compressatorium_api_key
        Bearer token, when the service has auth enabled.
    dats
        ROMarr's loaded DAT index. With None (or an empty index) nothing is
        ever deleted, whatever delete_originals says -- verification is the
        gate and without DATs the gate stays shut.
    delete_originals
        Delete each compressed original after its outputs verify. False by
        default; the API requires an explicit true in the request body.
    output_dir
        Where outputs are written. Default: alongside each source, which is
        what the service does when the field is omitted.
    job_timeout
        Seconds to wait for the whole batch to reach a terminal state.
    poll_interval
        Seconds between status polls.
    dry_run
        Scan and report without submitting anything.
    """
    report = DecompressReport()
    t0 = time.monotonic()

    # -- scan -----------------------------------------------------------
    files = scan_compressed(directory)
    report.scanned = len(files)
    log.info("decompress: scanned %s, found %d compressed file(s)",
             directory, len(files))

    if not files:
        report.elapsed_seconds = time.monotonic() - t0
        return report

    if dry_run:
        for fpath in files:
            item = DecompressItem(source=str(fpath), status="skipped",
                                  mode=mode_for(fpath.name))
            item.dat_detail = "dry run -- no jobs submitted"
            report.items.append(item)
            report.skipped += 1
        report.elapsed_seconds = time.monotonic() - t0
        log.info("decompress: dry run, %d file(s) would be processed",
                 len(files))
        return report

    # -- the deletion gate ----------------------------------------------
    #
    # Decided once, here, rather than at each deletion site: deleting an
    # original is only ever justified by a DAT match, and with no index
    # loaded there is nothing that could produce one.
    can_delete = bool(delete_originals) and dats is not None and bool(dats.dats)
    if delete_originals and not can_delete:
        log.warning("decompress: delete_originals requested but no DATs are "
                    "loaded -- originals will be KEPT regardless of outcome")

    # -- set up client --------------------------------------------------
    if not compressatorium_url:
        report.error = "no compressatorium URL configured"
        report.elapsed_seconds = time.monotonic() - t0
        return report

    client = CompressatoriumClient(CompressatoriumConfig(
        base_url=compressatorium_url,
        api_key=compressatorium_api_key,
    ))

    if not client.reachable():
        report.error = (f"compressatorium at {compressatorium_url} is not "
                        f"reachable")
        report.elapsed_seconds = time.monotonic() - t0
        return report

    # -- submit, one job per file ---------------------------------------
    #
    # Per-file jobs rather than /api/jobs/batch: the batch route takes ONE
    # mode for every file in the call, and a real library mixes CHD, RVZ,
    # 7z and NSZ in one directory. Grouping by mode to use the batch route
    # would buy one HTTP call per group and cost the per-file error
    # reporting that makes a failed batch diagnosable.
    items: list[DecompressItem] = []
    for fpath in files:
        item = DecompressItem(source=str(fpath), mode=mode_for(fpath.name))
        result: JobResult = client.submit_job(
            file_path=str(fpath),
            mode=item.mode,
            output_dir=str(output_dir) if output_dir else None,
            duplicate_action="skip",
        )
        if not result.job_id:
            item.status = "failed"
            item.error = result.error or "submission failed"
            report.failed += 1
            log.warning("decompress: submit failed for %s: %s",
                        fpath.name, item.error)
        else:
            item.status = "submitted"
            item.job_id = result.job_id
            report.submitted += 1
            log.info("decompress: submitted %s as job %s (%s)",
                     fpath.name, result.job_id, item.mode)
        items.append(item)

    # -- wait for completion --------------------------------------------
    job_ids = [it.job_id for it in items if it.job_id]
    final = client.wait_for_jobs(job_ids, timeout=job_timeout,
                                 poll_interval=poll_interval)
    for item in items:
        if item.status != "submitted":
            continue
        status = final.get(item.job_id)
        if status is None:
            item.status = "failed"
            item.error = "job vanished from the service"
            report.failed += 1
        elif status.status == COMPLETED:
            item.status = "decompressed"
            item.output_files = _output_files(status.output_path)
            report.decompressed += 1
            log.info("decompress: job %s completed -> %d file(s)",
                     item.job_id, len(item.output_files))
        elif status.status == FAILED:
            item.status = "failed"
            item.error = status.error or "job failed"
            report.failed += 1
            log.warning("decompress: job %s failed: %s",
                        item.job_id, item.error)
        else:
            # cancelled, or still running at the deadline
            item.status = "failed"
            item.error = (f"job ended as {status.status!r}"
                          if status.status == CANCELLED else
                          f"still {status.status} after {job_timeout}s")
            report.failed += 1
            log.warning("decompress: job %s: %s", item.job_id, item.error)

    # -- verify against DATs --------------------------------------------
    if dats is not None and dats.dats:
        for item in items:
            if item.status != "decompressed":
                continue
            status, detail = _verify_outputs(item.output_files, dats, client)
            item.dat_match = status
            item.dat_detail = detail
            if status == VERIFIED:
                item.status = "verified"
                report.verified += 1
                log.info("decompress: %s verified against DAT",
                         Path(item.source).name)
            elif status == BAD_DUMP:
                item.status = "failed"
                item.error = f"DAT verification: {detail}"
                report.failed += 1
                log.warning("decompress: %s is a BAD DUMP: %s",
                            Path(item.source).name, detail)
            else:
                # Unknown: extracted fine, but no DAT claims it. Kept, never
                # deleted -- see the module docstring for why.
                item.status = "kept"
                report.kept += 1
                log.info("decompress: %s extracted but unknown to the DATs; "
                         "original kept", Path(item.source).name)
    else:
        for item in items:
            if item.status == "decompressed" and item.output_files:
                item.status = "kept"
                item.dat_match = UNKNOWN
                item.dat_detail = ("no DATs loaded -- verification skipped, "
                                   "originals kept")
                report.kept += 1
                log.warning("decompress: %s decompressed but no DATs loaded "
                            "for verification", Path(item.source).name)

    # -- delete originals (only when gated open AND this item verified) --
    if can_delete:
        for item in items:
            if item.status != "verified" or item.dat_match != VERIFIED:
                continue
            source_path = Path(item.source)
            if not source_path.is_file():
                continue
            try:
                source_path.unlink()
                item.deleted = True
                item.status = "deleted"
                report.deleted += 1
                log.info("decompress: deleted original %s (verified: %s)",
                         source_path.name, item.dat_detail)
            except OSError as err:
                item.error = f"could not delete original: {err}"
                log.warning("decompress: could not delete %s: %s",
                            source_path.name, err)
    elif any(it.status == "verified" for it in items):
        log.info("decompress: %d file(s) verified but originals kept "
                 "(deletion not authorised)",
                 sum(1 for it in items if it.status == "verified"))

    report.items = items
    report.elapsed_seconds = time.monotonic() - t0
    log.info("decompress: batch complete -- %d scanned, %d submitted, "
             "%d verified, %d kept, %d failed, %d deleted in %.1fs",
             report.scanned, report.submitted, report.verified, report.kept,
             report.failed, report.deleted, report.elapsed_seconds)
    return report
