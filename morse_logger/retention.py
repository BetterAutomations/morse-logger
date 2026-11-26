"""Retention: archive or delete what is older than N days.

Nothing is removed unless asked: `retention_days` defaults to 0 (keep
everything), and `prune` needs an explicit `--days`. Archive is the default
action - old text is gzipped into `<home>/archive/` rather than deleted -
because a log pruned last month is often the one wanted this month. `--delete`
removes instead, for people who really mean it.

Covered: the daily text files, the JSONL (rewritten atomically, old lines
archived) and the SQLite `captures` rows. Both canonical stores are pruned to
the same cutoff, so the views never disagree. The daemon must not be running:
it holds the JSONL open and would keep writing into a file that was replaced.
"""
import glob
import gzip
import json
import os
import sqlite3
import shutil
from datetime import datetime, timedelta

TEXT_PREFIX = "morse-"
JSONL = "morse.jsonl"
DB = "morse.sqlite3"


def _archive_dir(home):
    d = os.path.join(home, "archive")
    os.makedirs(d, exist_ok=True)
    return d


def _gzip_file(src, dest_dir):
    size = os.path.getsize(src)
    name = os.path.basename(src)
    dest = os.path.join(dest_dir, name + ".gz")
    tmp = dest + ".part"
    with open(src, "rb") as fin, gzip.open(tmp, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    os.replace(tmp, dest)
    return size


def _daily_date(name):
    """2026-11-26 from morse-2026-11-26.txt, or None."""
    if not (name.startswith(TEXT_PREFIX) and name.endswith(".txt")):
        return None
    try:
        return datetime.strptime(name[len(TEXT_PREFIX):-4], "%Y-%m-%d")
    except ValueError:
        return None


def _record_date(obj):
    """The day a JSONL record belongs to, as YYYY-MM-DD, or None."""
    stamp = obj.get("logged_at") or obj.get("started_utc") or obj.get("ts") or ""
    return stamp[:10] if len(stamp) >= 10 else None


def prune(home, log_dir, days, dry_run=False, delete=False):
    """Remove or archive everything older than `days`. Returns a summary."""
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_day = cutoff.strftime("%Y-%m-%d")
    arch = None if dry_run else _archive_dir(home)
    out = {"days": days, "cutoff": cutoff_day, "files": 0, "bytes": 0,
           "jsonl_lines": 0, "rows": 0, "notes": []}

    def handle(path):
        out["files"] += 1
        out["bytes"] += os.path.getsize(path)
        if dry_run:
            return
        if delete:
            os.remove(path)
        else:
            _gzip_file(path, arch)
            os.remove(path)

    # 1. Daily text files, dated by name (an edit does not make them young).
    for path in sorted(glob.glob(os.path.join(log_dir, TEXT_PREFIX + "*.txt"))):
        day = _daily_date(os.path.basename(path))
        if day is not None and day < cutoff:
            handle(path)

    # 2. JSONL: one append-only file, so rewrite it and archive the old lines.
    jsonl = os.path.join(log_dir, JSONL)
    if os.path.exists(jsonl):
        keep, old = [], []
        with open(jsonl, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    day = _record_date(json.loads(line))
                except ValueError:
                    keep.append(line)          # unparsable: never silently drop
                    continue
                (old if day is not None and day < cutoff_day else keep).append(line)
        if old:
            out["jsonl_lines"] = len(old)
            out["bytes"] += os.path.getsize(jsonl)
            if not dry_run:
                dest = os.path.join(arch, f"{JSONL}.{cutoff_day}.gz")
                with gzip.open(dest + ".part", "wt", encoding="utf-8") as fh:
                    fh.writelines(old)
                os.replace(dest + ".part", dest)
                tmp = jsonl + ".part"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.writelines(keep)
                os.replace(tmp, jsonl)

    # 3. SQLite: same cutoff, then give the space back.
    db_path = os.path.join(log_dir, DB)
    if os.path.exists(db_path):
        db = sqlite3.connect(db_path, timeout=10)
        try:
            has_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='captures'"
            ).fetchone()
            if has_table:
                n = db.execute(
                    "SELECT COUNT(*) FROM captures WHERE substr(started_utc, 1, 10) < ?",
                    (cutoff_day,)).fetchone()[0]
                out["rows"] = n
                if n and not dry_run:
                    db.execute(
                        "DELETE FROM captures WHERE substr(started_utc, 1, 10) < ?",
                        (cutoff_day,))
                    db.commit()
                    db.execute("VACUUM")
        finally:
            db.close()

    out["delete"] = bool(delete)
    out["dry_run"] = bool(dry_run)
    return out


def summary_text(result):
    verb = "deleted" if result["delete"] else "archived"
    lines = [
        f"cutoff       {result['cutoff']} ({result['days']} days)",
        f"files        {result['files']} {verb}",
        f"jsonl lines  {result['jsonl_lines']} {verb}",
        f"db rows      {result['rows']} {'deleted' if not result['dry_run'] else 'would delete'}",
        f"bytes        {result['bytes']}",
    ]
    if result["dry_run"]:
        lines.insert(0, "dry run - nothing was touched")
    return "\n".join(lines)
