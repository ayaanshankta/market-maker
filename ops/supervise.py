"""Keep recorder.py running; stop it gracefully (Ctrl-C -> flush) when a STOP file appears.

Launched with pythonw.exe by the Windows scheduled task "market-maker-recorder" (at logon + every
5 min; a second copy exits at once because of the lock).  Stop: create STOP in the repo dir.
Start: delete STOP, then Start-ScheduledTask market-maker-recorder (or wait <= 5 min).
"""
import ctypes, logging, msvcrt, os, signal, subprocess, time
from pathlib import Path

REPO = Path(r"D:\projects\market-maker")
PY = REPO / ".venv" / "Scripts" / "python.exe"
STOP = REPO / "STOP"
LOCK = REPO / "supervisor.lock"

log = logging.getLogger("supervisor")


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
    k32 = ctypes.windll.kernel32
    # Run under pythonw (no console): this gives us a fresh private console, so the Ctrl-C below
    # can only reach us and the recorder.
    if not k32.AllocConsole():
        log.error("AllocConsole failed (launched with python.exe?); refusing to run")
        return
    ctypes.windll.user32.ShowWindow(k32.GetConsoleWindow(), 0)
    k32.SetConsoleCtrlHandler(None, False)  # re-enable Ctrl-C in case our launcher disabled it (inherited)
    backoff = 10
    while True:
        started = time.monotonic()
        p = subprocess.Popen([str(PY), "recorder.py"], cwd=REPO,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("recorder started pid=%d", p.pid)
        while p.poll() is None and not STOP.exists():
            time.sleep(1)
        if p.poll() is None:  # STOP requested
            log.info("STOP found; sending Ctrl-C to pid=%d", p.pid)
            k32.SetConsoleCtrlHandler(None, True)  # OS-level ignore for us only; Python's SIG_IGN wasn't enough
            os.kill(0, signal.CTRL_C_EVENT)  # group 0 = this private console: us + recorder only
            try:
                log.info("recorder exited rc=%s after Ctrl-C", p.wait(timeout=90))
            except subprocess.TimeoutExpired:
                p.kill()
                log.error("recorder ignored Ctrl-C for 90 s; killed (unflushed rows lost)")
            return
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
