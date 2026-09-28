"""Keep recorder.py running; stop it gracefully (Ctrl-C -> flush) when a STOP file appears.

Launched with pythonw.exe by the Windows scheduled task "market-maker-recorder" (at logon + every
5 min; a second copy exits at once because of the lock).  Stop: create STOP in the repo dir.
Start: delete STOP, then Start-ScheduledTask market-maker-recorder (or wait <= 5 min).

Watchdog: a recorder that is alive but writes no new data for WATCHDOG_S is restarted (at most
WATCHDOG_MAX times per hour; after that an ALERT-watchdog-*.txt flag is written and it stops trying).
MM_* environment variables override the defaults; they exist only so the watchdog can be tested
against a scratch directory.
"""
import ctypes, logging, msvcrt, os, signal, subprocess, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(os.environ.get("MM_REPO", r"C:\projects\market-maker"))
PY = Path(r"C:\projects\market-maker\.venv\Scripts\python.exe")
STOP = REPO / "STOP"
LOCK = REPO / "supervisor.lock"
CHECK_S = float(os.environ.get("MM_CHECK_S", 60))
WATCHDOG_S = float(os.environ.get("MM_WATCHDOG_S", 300))
WATCHDOG_MAX = int(os.environ.get("MM_WATCHDOG_MAX", 3))
KILL_S = float(os.environ.get("MM_KILL_S", 30))

log = logging.getLogger("supervisor")
k32 = ctypes.windll.kernel32


def newest_data_mtime():
    # Files land in the UTC hour of their first row, so the current and previous hour cover "now".
    now = datetime.now(timezone.utc)
    best = 0.0
    for t in (now, now - timedelta(hours=1)):
        for f in (REPO / "data").glob(f"*/*/*/date={t:%Y-%m-%d}/hour={t:%H}/*.parquet"):
            best = max(best, f.stat().st_mtime)
    return best


def stop(p, timeout):
    """Ctrl-C the recorder (it flushes); kill it if it hasn't exited after `timeout` s."""
    k32.SetConsoleCtrlHandler(None, True)  # OS-level ignore for us only; Python's SIG_IGN wasn't enough
    os.kill(0, signal.CTRL_C_EVENT)  # group 0 = this private console: us + recorder only
    try:
        rc = p.wait(timeout=timeout)
        log.info("recorder exited rc=%s after Ctrl-C", rc)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        log.error("recorder ignored Ctrl-C for %.0f s; killed (unflushed rows lost)", timeout)
    k32.SetConsoleCtrlHandler(None, False)  # the ignore flag is inherited; the next recorder must get Ctrl-C


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(REPO / "supervisor.log")])
    lock = open(LOCK, "a")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return  # another supervisor already owns the recorder
    if STOP.exists():
        log.info("STOP present; not starting")
        return
    # Run under pythonw (no console): this gives us a fresh private console, so the Ctrl-C below
    # can only reach us and the recorder.
    if not k32.AllocConsole():
        log.error("AllocConsole failed (launched with python.exe?); refusing to run")
        return
    ctypes.windll.user32.ShowWindow(k32.GetConsoleWindow(), 0)
    k32.SetConsoleCtrlHandler(None, False)  # re-enable Ctrl-C in case our launcher disabled it (inherited)
    backoff, restarts, capped = 10, [], False
    while True:
        started, started_wall = time.monotonic(), time.time()
        # stderr is kept so an uncaught traceback survives a crash (it also duplicates recorder.log lines).
        with open(REPO / "recorder.stderr.log", "a") as err:
            p = subprocess.Popen([str(PY), "recorder.py"], cwd=REPO, stdout=subprocess.DEVNULL, stderr=err)
        log.info("recorder started pid=%d", p.pid)
        next_check, watchdog_stopped = time.monotonic() + CHECK_S, False
        while p.poll() is None and not STOP.exists():
            time.sleep(1)
            if time.monotonic() < next_check:
                continue
            next_check = time.monotonic() + CHECK_S
            # Measured from the later of the newest file and this recorder's start, so a fresh
            # start after downtime isn't mistaken for a hang.
            idle = time.time() - max(newest_data_mtime(), started_wall)
            if idle <= WATCHDOG_S:
                continue
            now = time.monotonic()
            restarts = [t for t in restarts if now - t < 3600]
            if len(restarts) >= WATCHDOG_MAX:
                if not capped:
                    capped = True
                    flag = REPO / f"ALERT-watchdog-{datetime.now():%Y-%m-%d-%H%M}.txt"
                    msg = (f"WATCHDOG: no new data for {idle:.0f} s after {WATCHDOG_MAX} watchdog restarts "
                           f"in the last hour; not restarting again (network down?)")
                    flag.write_text(msg + "\n")
                    log.error("%s; wrote %s", msg, flag.name)
                continue
            capped = False
            restarts.append(now)
            log.warning("WATCHDOG: no new data for %.0f s, restarting (%d/%d this hour)",
                        idle, len(restarts), WATCHDOG_MAX)
            stop(p, KILL_S)
            watchdog_stopped = True
            break
        if STOP.exists():
            if p.poll() is None:
                log.info("STOP found; sending Ctrl-C to pid=%d", p.pid)
                stop(p, 90)
            return
        if not watchdog_stopped:
            log.error("recorder exited unexpectedly rc=%s after %.0f s", p.returncode, time.monotonic() - started)
            if time.monotonic() - started > 600:
                backoff = 10
            for _ in range(backoff):
                if STOP.exists():
                    return
                time.sleep(1)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
