"""Daily 06:00 job: check yesterday (UTC), then (if the check passed) tob.py + eda.py and walkforward.py for both
venues, commit one line to coord/recorder-health.md plus reports/ (eda-<day>*, walkforward.csv) on branch
desktop, and only then back up data/ and derived/
to D: (a slow external disk must not delay the check), committing the backup result as a follow-up line.

Run by the scheduled task "market-maker-daily" with pythonw.  Optional arg: a UTC day (YYYY-MM-DD).
Anything alert-worthy gets an "ALERT" prefix in the health line plus a flag file ALERT-<day>.txt in the repo.
An eda/tob failure is only noted ("eda FAILED"); failing two days running is an ALERT.
"""
import logging, os, re, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq

import pids

REPO = Path(r"C:\projects\market-maker")
WT = Path(r"C:\projects\market-maker-desktop")
DATA = REPO / "data"
BACKUP = Path(r"D:\market-maker-backup\data")
DERIVED, BACKUP_DERIVED = REPO / "derived", Path(r"D:\market-maker-backup\derived")
REPORTS, WT_REPORTS = REPO / "reports", WT / "reports"
HEALTH = WT / "coord" / "recorder-health.md"
PY = REPO / ".venv" / "Scripts" / "python.exe"
FIRST_DAY = "2026-09-27"  # first UTC day with data
DOWNTIME_ALERT_S = 30 * 60
HEADROOM_ALERT_DAYS = 7
ROLLOVER_GAP_ALERT_S = 5  # Kraken needs ~2.8 s at 00:00 UTC (1 s capped close + reconnect/snapshot); 12 s was the failure
FLAGS = 0x00004000 | 0x08000000  # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW

log = logging.getLogger("daily")


def run(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, creationflags=FLAGS, **kw)
    log.info("%s -> rc %s", " ".join(map(str, args)), r.returncode)
    return r


def backup(alerts, src=DATA, dst=BACKUP, what="backup"):
    if not Path("D:/").exists():
        alerts.append(f"{what}: D: missing (unplugged?)")
        return f"{what} SKIPPED (D: missing)"
    # /E /XO only: never /MIR or /PURGE -- the backup must never delete anything.
    r = run(["robocopy", str(src), str(dst), "/E", "/XO", "/R:2", "/W:5", "/NP", "/NFL", "/NDL",
             f"/LOG+:{REPO / 'backup.log'}"])
    if r.returncode >= 8:
        alerts.append(f"{what}: robocopy rc {r.returncode}")
    return f"{what} rc {r.returncode}"


def day_bytes(day):
    return sum(f.stat().st_size for f in DATA.glob(f"*/*/*/date={day}/*/*.parquet"))


def check(day, alerts):
    r = run([str(PY), "check.py", day], cwd=REPO)
    out = r.stdout
    log.info("check.py output:\n%s%s", out, r.stderr)
    sections = re.findall(r"^== (\w+) (\S+): book rows ([\d,]+)  trade rows ([\d,]+)", out, re.M)
    num = lambda s: int(s.replace(",", ""))
    book = sum(num(b) for *_, b, _ in sections)
    trades = sum(num(t) for *_, t in sections)
    crossed = sum(map(int, re.findall(r"crossed-book messages (\d+)", out)))
    ok = sum(num(a) for a, _ in re.findall(r"checksums matched ([\d,]+)/([\d,]+)", out))
    tot = sum(num(b) for _, b in re.findall(r"checksums matched ([\d,]+)/([\d,]+)", out))
    # Downtime per venue = gaps in its BTC-USD book (always busy). check.py lists at most 20 gaps.
    gaps = {}
    for block in re.split(r"\n(?=== )", out):
        m = re.match(r"\s*== (\w+) BTC-USD:", block)
        if m:
            n = int(re.search(r"recording gaps > 5s: (\d+)", block).group(1))
            secs = sum(map(int, re.findall(r"\((\d+)s\)", block)))
            gaps[m.group(1)] = (n, secs, n > 20)
    unverified = re.findall(r"UNVERIFIED rows ([\d,]+)", out)
    if r.returncode != 0:
        alerts.append(f"check.py rc {r.returncode}")
    if unverified and day > FIRST_DAY:  # with the 00:00 UTC rollover every day should be fully seeded
        alerts.append(f"UNVERIFIED rows {sum(num(u) for u in unverified):,}")
    if not sections:
        alerts.append("no data for day")
    for v, (n, secs, partial) in gaps.items():
        if secs > DOWNTIME_ALERT_S or partial:
            alerts.append(f"{v} downtime {'>=' if partial else ''}{secs} s over {n} gaps")
    g = ", ".join(f"{v} {n} ({'>=' if p else ''}{s} s)" for v, (n, s, p) in sorted(gaps.items()))
    return (f"rows {book + trades:,} (book {book:,}, trades {trades:,}) | gaps>5s: {g or 'n/a'} | "
            f"checksums {ok:,}/{tot:,} | crossed {crossed} | "
            f"unverified {sum(num(u) for u in unverified):,} | check rc {r.returncode}"), \
        r.returncode == 0 and bool(sections)


def failed_yesterday(day, marker):
    prev = str(datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1))
    if not HEALTH.exists():
        return False
    return any(re.search(rf"(^- |\| ){prev} \|", l) and marker in l for l in HEALTH.read_text().splitlines())


def step(args, label, day, alerts):
    """Run a repo script via ops/peakrun.py (BelowNormal; runtime + peak memory to daily.log).
    Returns None on success, else '<label> FAILED: <reason>' (an ALERT only if it also failed yesterday)."""
    r = run([str(PY), str(WT / "ops" / "peakrun.py"), *args], cwd=REPO)
    stats = re.search(r"PEAKRUN .*", r.stderr)
    log.info("%s", stats[0] if stats else f"{args[0]}: no PEAKRUN stats")
    log.info("%s stdout:\n%s", " ".join(args), r.stdout)
    if r.returncode == 0:
        return None
    log.info("%s stderr:\n%s", " ".join(args), r.stderr)
    tail = [l for l in r.stderr.splitlines() if l.strip() and not l.startswith("PEAKRUN")]
    reason = f"{' '.join(args)} rc {r.returncode}: {tail[-1][:120] if tail else 'no stderr'}"
    if failed_yesterday(day, f"{label} FAILED"):
        alerts.append(f"{label} failed 2 days running ({reason})")
    return f"{label} FAILED: {reason}"


def walkforward(day, check_ok, alerts):
    # Train on day-1, test on day, 6 fixed configs; rows appended to reports/walkforward.csv (~30-40 min).
    if not check_ok:
        return "walkforward skipped (check failed)"
    for venue in ("coinbase", "kraken"):
        failed = step(["walkforward.py", day, venue, "SOL-USD"], "walkforward", day, alerts)
        if failed:
            return failed
    WT_REPORTS.mkdir(exist_ok=True)
    shutil.copy2(REPORTS / "walkforward.csv", WT_REPORTS / "walkforward.csv")
    return "walkforward ok"


def phase2(day, check_ok, alerts):
    # tob.py then eda.py, BelowNormal, each wrapped by ops/peakrun.py for runtime + peak memory in daily.log.
    if not check_ok:
        return "eda skipped (check failed)"
    for script in ("tob.py", "eda.py"):
        failed = step([script, day], "eda", day, alerts)
        if failed:
            return failed
    WT_REPORTS.mkdir(exist_ok=True)
    reports = sorted(REPORTS.glob(f"eda-{day}*"))
    for f in reports:
        shutil.copy2(f, WT_REPORTS / f.name)
    return f"eda ok ({len(reports)} report files)"


def reconnects(day, alerts):
    # recorder.log timestamps are local time; the day is UTC. One "daily UTC rollover" per venue is expected.
    rollovers, other = {}, []
    for line in (REPO / "recorder.log").read_text(errors="replace").splitlines():
        m = re.match(r"(\S+ \S+),\d+ WARNING (\w+): disconnected: (.*)", line)
        if not m or str(datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc).date()) != day:
            continue
        if "daily UTC rollover" in m[3]:
            rollovers[m[2]] = rollovers.get(m[2], 0) + 1
        else:
            other.append(f"{m[2]} {m[3][:80]}")
    s = f"rollovers {', '.join(f'{v} {n}' for v, n in sorted(rollovers.items())) or 0} | other disconnects {len(other)}"
    return s + (f" ({'; '.join(other[:5])}{' ...' if len(other) > 5 else ''})" if other else "")


def rollover_start(day, alerts):
    # The day must open on a fresh connection: each book's first row is a snapshot, and the
    # switch from yesterday's last row to today's first is short (per venue, BTC-USD book).
    if day <= FIRST_DAY:
        return "rollover n/a"
    prev = str(datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1))
    first = lambda d, v, s: sorted((DATA / v / s / "book").glob(f"date={d}/hour=*/*.parquet"), key=lambda f: int(f.stem))
    not_snap, gaps = [], []
    for sec in sorted(p for p in DATA.glob("*/*") if p.is_dir()):
        v, s = sec.parent.name, sec.name
        files = first(day, v, s)
        if not files:
            continue
        t = pq.read_table(files[0], columns=["recv_ns", "is_snapshot"])
        if not t["is_snapshot"][0].as_py():
            not_snap.append(f"{v} {s}")
        if s == "BTC-USD":
            before = first(prev, v, s)
            if before:
                last = max(pq.read_table(before[-1], columns=["recv_ns"])["recv_ns"].to_pylist())
                gap = (min(t["recv_ns"].to_pylist()) - last) / 1e9
                gaps.append(f"{v} {gap:.2f} s")
                if gap > ROLLOVER_GAP_ALERT_S:
                    alerts.append(f"{v} rollover gap {gap:.1f} s")
    if not_snap:
        alerts.append(f"day starts without snapshot: {', '.join(not_snap)}")
    return f"rollover gap {', '.join(gaps) or 'n/a'}, first rows snapshot: {'NO ' + ', '.join(not_snap) if not_snap else 'all'}"


def disk(day, alerts):
    per_day = day_bytes(day) / 1e9
    free_c = shutil.disk_usage("C:/").free / 1e9
    s = f"{per_day:.2f} GB/day, C: {free_c:.0f} GB free"
    if per_day > 0:
        s += f" = {free_c / per_day:.0f} d"
        if free_c / per_day < HEADROOM_ALERT_DAYS:
            alerts.append(f"C: headroom {free_c / per_day:.1f} d")
        if Path("D:/").exists():
            free_d = shutil.disk_usage("D:/").free / 1e9
            s += f"; D: {free_d:.0f} GB free = {free_d / per_day:.0f} d"
            if free_d / per_day < HEADROOM_ALERT_DAYS:
                alerts.append(f"D: backup headroom {free_d / per_day:.1f} d")
    return s


def publish(line, day, alerts, what="health"):
    git = lambda *a: run(["git", "-C", str(WT), *a])
    HEALTH.parent.mkdir(exist_ok=True)
    if not HEALTH.exists():
        HEALTH.write_text("# Recorder health (one line per UTC day, written by ops/daily.py at 06:00 local)\n\n")
    with HEALTH.open("a") as f:
        f.write(f"- {line}\n")
    git("add", str(HEALTH))
    if WT_REPORTS.exists():
        git("add", str(WT_REPORTS))
    git("commit", "-q", "-m", f"{what}: {day}{' ALERT' if alerts else ''}")
    if git("push", "-q", "origin", "desktop").returncode != 0:
        git("pull", "-q", "--rebase", "origin", "desktop")
        if git("push", "-q", "origin", "desktop").returncode != 0:
            alerts.append("git push failed")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(REPO / "daily.log")])
    dry = "--dry-run" in sys.argv  # print the line; no backup, commit or flag file
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    day = args[0] if args else str((datetime.now(timezone.utc) - timedelta(days=1)).date())
    alerts = []
    if not dry:
        pids.update(REPO, "daily job", [f"updated {datetime.now():%Y-%m-%d %H:%M:%S}"]
                    + pids.describe([os.getppid(), os.getpid()])
                    + ["(plus short-lived children: check.py, tob.py, eda.py, robocopy, git)"])
    try:
        c, check_ok = check(day, alerts)
        r = reconnects(day, alerts)
        ro = rollover_start(day, alerts)
        e = "eda skipped (dry run)" if dry else phase2(day, check_ok, alerts)
        wf = "walkforward skipped (dry run)" if dry else walkforward(day, check_ok, alerts)
        d = disk(day, alerts)
        line = f"{day} | {c} | {r} | {ro} | {e} | {wf} | {d} | backup: next line"
        if alerts:
            line = f"ALERT ({'; '.join(alerts)}) | {line}"
        log.info("health: %s", line)
        if dry:
            print(line)
            return
        publish(line, day, alerts)
        if alerts:
            (REPO / f"ALERT-{day}.txt").write_text(line + "\n")
        # Backups last: a slow external disk must never hold up the check or the analysis above.
        balerts = []
        b = backup(balerts)
        bd = backup(balerts, DERIVED, BACKUP_DERIVED, "derived backup")
        follow = f"{day} | {b} | {bd} | finished {datetime.now():%H:%M} local"
        if balerts:
            follow = f"ALERT ({'; '.join(balerts)}) | {follow}"
            (REPO / f"ALERT-{day}-backup.txt").write_text(follow + "\n")
        log.info("health: %s", follow)
        publish(follow, day, balerts, "backup")
    finally:
        if not dry:
            pids.update(REPO, "daily job", None)


if __name__ == "__main__":
    main()
