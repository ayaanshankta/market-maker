"""Daily 06:00 job: back up new data to D:, check yesterday (UTC), append one line to coord/recorder-health.md.

Run by the scheduled task "market-maker-daily" with pythonw.  Optional arg: a UTC day (YYYY-MM-DD).
Anything alert-worthy gets an "ALERT" prefix in the health line plus a flag file ALERT-<day>.txt in the repo.
"""
import logging, re, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq

REPO = Path(r"C:\projects\market-maker")
WT = Path(r"C:\projects\market-maker-desktop")
DATA = REPO / "data"
BACKUP = Path(r"D:\market-maker-backup\data")
HEALTH = WT / "coord" / "recorder-health.md"
PY = REPO / ".venv" / "Scripts" / "python.exe"
FIRST_DAY = "2026-09-27"  # first UTC day with data
DOWNTIME_ALERT_S = 30 * 60
HEADROOM_ALERT_DAYS = 7
FLAGS = 0x00004000 | 0x08000000  # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW

log = logging.getLogger("daily")


def run(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, creationflags=FLAGS, **kw)
    log.info("%s -> rc %s", " ".join(map(str, args)), r.returncode)
    return r


def backup(alerts):
    if not Path("D:/").exists():
        alerts.append("backup: D: missing (unplugged?)")
        return "backup SKIPPED (D: missing)"
    # /E /XO only: never /MIR or /PURGE -- the backup must never delete anything.
    r = run(["robocopy", str(DATA), str(BACKUP), "/E", "/XO", "/R:2", "/W:5", "/NP", "/NFL", "/NDL",
             f"/LOG+:{REPO / 'backup.log'}"])
    if r.returncode >= 8:
        alerts.append(f"backup: robocopy rc {r.returncode}")
    return f"backup rc {r.returncode}"


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
            f"unverified {sum(num(u) for u in unverified):,} | check rc {r.returncode}")


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
                if gap > 2:
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


def publish(line, day, alerts):
    git = lambda *a: run(["git", "-C", str(WT), *a])
    HEALTH.parent.mkdir(exist_ok=True)
    if not HEALTH.exists():
        HEALTH.write_text("# Recorder health (one line per UTC day, written by ops/daily.py at 06:00 local)\n\n")
    with HEALTH.open("a") as f:
        f.write(f"- {line}\n")
    git("add", str(HEALTH))
    git("commit", "-q", "-m", f"health: {day}{' ALERT' if alerts else ''}")
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
    b = "backup skipped (dry run)" if dry else backup(alerts)
    log.info("%s", b)
    if day < FIRST_DAY:
        log.info("%s is before first recording day %s; no check", day, FIRST_DAY)
        if alerts:
            (REPO / f"ALERT-{day}.txt").write_text(f"ALERT ({'; '.join(alerts)}) | {day} | {b}\n")
        return
    c = check(day, alerts)
    r = reconnects(day, alerts)
    ro = rollover_start(day, alerts)
    d = disk(day, alerts)
    line = f"{day} | {c} | {r} | {ro} | {d} | {b}"
    if alerts:
        line = f"ALERT ({'; '.join(alerts)}) | {line}"
    log.info("health: %s", line)
    if dry:
        print(line)
        return
    publish(line, day, alerts)
    if alerts:
        (REPO / f"ALERT-{day}.txt").write_text(line + "\n")


if __name__ == "__main__":
    main()
