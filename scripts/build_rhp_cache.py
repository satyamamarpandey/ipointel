#!/usr/bin/env python
"""Memory-guarded bulk cache of India RHP page text (local ops script, not app code).

    python scripts/build_rhp_cache.py 1            # one worker, resumes
    python scripts/build_rhp_cache.py 1 --retry-invalid
    python scripts/backfill_india_rhp.py --cache-dir data/rhp_cache/pages --limit 2000

Everything lives in data/rhp_cache/ (gitignored): pages/<SYMBOL>.json, state.json
(per-symbol status), symbols.db (prod data-state copy that lists expected symbols).

- Resumes from rhp_pages/*.json; never touches cached files.
- Streams each archive to disk; a child process (one archive per child, then
  it exits) unzips the PDF to disk and extracts page text, so memory is
  returned to the OS after every archive.
- Before scheduling each archive: pause while available RAM < MIN_AVAIL_MB or
  remaining commit < MIN_COMMIT_MB (up to MAX_WAIT_S, then exit 3); drop from
  2 workers to 1 below DOWNGRADE_AVAIL_MB. Drains in-flight work before exiting.
- State in state.json (per symbol classification), written after each item.
"""
import ctypes
import json
import os
import sqlite3
import sys
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
S = ROOT / "data" / "rhp_cache"
CACHE = S / "pages"
TMP = S / "tmp"
STATE = S / "state.json"
DB = S / "symbols.db"
MIN_AVAIL_MB = 1500
MIN_COMMIT_MB = 2500
DOWNGRADE_AVAIL_MB = 2500
CHECKPOINT_EVERY = 25
WAIT_STEP_S = 15
MAX_WAIT_S = 3600


class MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def mem() -> tuple[int, int]:
    m = MEMSTAT()
    m.dwLength = ctypes.sizeof(MEMSTAT)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPhys // 2**20, m.ullAvailPageFile // 2**20


def work(sym: str) -> tuple[str, str, str]:
    """(symbol, classification, detail). Runs in a short-lived child."""
    import httpx
    from app.services import rhp_financials as rf
    zpath, ppath = TMP / f"{sym}.zip", TMP / f"{sym}.pdf"
    try:
        url = rf.RHP_URL.format(symbol=sym)
        size = 0
        with httpx.Client(headers=rf._UA, timeout=120, follow_redirects=True) as c, c.stream("GET", url) as r:
            if r.status_code == 404:
                return sym, "NOT_PUBLISHED", "http 404"
            if r.status_code in (403, 410):
                return sym, "DOWNLOAD_FAILED_PERMANENT", f"http {r.status_code}"
            if r.status_code >= 400:
                return sym, "DOWNLOAD_FAILED_RETRYABLE", f"http {r.status_code}"
            with open(zpath, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    size += len(chunk)
                    if size > rf.MAX_ZIP_BYTES:
                        return sym, "INVALID_DOCUMENT", "archive larger than cap"
                    f.write(chunk)
    except httpx.HTTPError as e:
        return sym, "DOWNLOAD_FAILED_RETRYABLE", type(e).__name__
    try:
        from pypdf import PdfReader
        with zipfile.ZipFile(zpath) as z:
            name = rf.pick_pdf(z.namelist(), lambda n: z.getinfo(n).file_size)
            if name is None:
                return sym, "INVALID_DOCUMENT", "no PDF in archive"
            with z.open(name) as src, open(ppath, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
        reader = PdfReader(str(ppath))
        pages = []
        for pg in reader.pages[:rf.SCAN_PAGES]:
            try:
                pages.append(pg.extract_text() or "")
            except Exception:
                pages.append("")
        del reader
        if not any(p.strip() for p in pages):
            return sym, "INVALID_DOCUMENT", "no extractable text (scanned?)"
        (CACHE / f"{sym}.json").write_text(json.dumps(pages), encoding="utf-8")
        return sym, "CACHED", f"{len(pages)} pages"
    except Exception as e:  # broken zip / PDF
        return sym, "INVALID_DOCUMENT", f"{type(e).__name__}: {str(e)[:80]}"
    finally:
        for p in (zpath, ppath):
            try:
                p.unlink()
            except OSError:
                pass


def expected() -> list[str]:
    c = sqlite3.connect(DB)
    rows = c.execute("select upper(symbol) from ipos where country='India' and status in ('Listed','Open','Closed','Upcoming') "
                     "and symbol is not null and symbol!='' order by listing_date desc").fetchall()
    c.close()
    return list(dict.fromkeys(r[0] for r in rows))


def main() -> int:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    retry_invalid = "--retry-invalid" in sys.argv
    CACHE.mkdir(exist_ok=True)
    TMP.mkdir(exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    syms = expected()
    for s in syms:
        if (CACHE / f"{s}.json").exists():
            state[s] = {"status": "CACHED", "detail": state.get(s, {}).get("detail", "from earlier run")}
    done_states = {"CACHED", "NOT_PUBLISHED", "DOWNLOAD_FAILED_PERMANENT"} | (set() if retry_invalid else {"INVALID_DOCUMENT"})
    todo = [s for s in syms if state.get(s, {}).get("status") not in done_states]
    print(f"expected {len(syms)} cached {sum(1 for s in syms if state.get(s, {}).get('status') == 'CACHED')} todo {len(todo)} workers {workers} mem {mem()}", flush=True)
    t0, n, stop_reason = time.time(), 0, "finished"
    pending: dict = {}
    queue = list(todo)
    ex = ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=1)
    try:
        while queue or pending:
            while queue and len(pending) < workers:
                avail, commit = mem()
                if (avail < MIN_AVAIL_MB or commit < MIN_COMMIT_MB) and pending:
                    break  # let in-flight work drain before re-checking
                waited = 0
                while (avail < MIN_AVAIL_MB or commit < MIN_COMMIT_MB) and waited < MAX_WAIT_S:
                    if waited % 300 == 0:
                        print(f"[{time.strftime('%H:%M:%S')}] pause: avail {avail} MB, commit left {commit} MB", flush=True)
                    time.sleep(WAIT_STEP_S)
                    waited += WAIT_STEP_S
                    avail, commit = mem()
                if avail < MIN_AVAIL_MB or commit < MIN_COMMIT_MB:
                    stop_reason = f"memory guard (waited {waited}s): avail {avail} MB, commit left {commit} MB"
                    queue = []
                    break
                if workers > 1 and avail < DOWNGRADE_AVAIL_MB:
                    workers = 1
                    print(f"downgrade to 1 worker (avail {avail} MB)", flush=True)
                    if len(pending) >= workers:
                        break
                s = queue.pop(0)
                pending[ex.submit(work, s)] = s
            if not pending:
                break
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for f in finished:
                s = pending.pop(f)
                try:
                    sym, status, detail = f.result()
                except Exception as e:  # child crashed (e.g. killed)
                    sym, status, detail = s, "DOWNLOAD_FAILED_RETRYABLE", f"worker error {type(e).__name__}"
                state[sym] = {"status": status, "detail": detail}
                STATE.write_text(json.dumps(state, indent=0))
                n += 1
                if n % CHECKPOINT_EVERY == 0:
                    checkpoint(state, syms, n, t0)
    finally:
        ex.shutdown(wait=True)
        STATE.write_text(json.dumps(state, indent=0))
    checkpoint(state, syms, n, t0)
    print("STOP:", stop_reason, flush=True)
    return 0 if stop_reason == "finished" else 3


def checkpoint(state, syms, n, t0):
    from collections import Counter
    c = Counter(state.get(s, {}).get("status", "PENDING") for s in syms)
    avail, commit = mem()
    disk = sum(p.stat().st_size for p in CACHE.glob("*.json")) // 2**20
    print(f"[{time.strftime('%H:%M:%S')}] processed {n} in {time.time() - t0:.0f}s | {dict(c)} | avail {avail} MB commit-left {commit} MB | cache {disk} MB", flush=True)


if __name__ == "__main__":
    sys.exit(main())
