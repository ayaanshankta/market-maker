"""Run a repo script in this interpreter and print its runtime and peak working set to stderr.

Usage (cwd = repo):  python ops/peakrun.py tob.py 2026-09-28
The venv python.exe is a launcher shim, so measuring from outside would see the shim, not the real work.
"""
import ctypes, ctypes.wintypes as w, runpy, sys, time, traceback
from pathlib import Path


class PMC(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


def peak_mib():
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    k32.GetCurrentProcess.restype = w.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(PMC), w.DWORD]
    c = PMC()
    c.cb = ctypes.sizeof(c)
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.PeakWorkingSetSize / 2**20


def main():
    script, repo = sys.argv[1], Path.cwd()
    sys.path.insert(0, str(repo))
    sys.argv = [script, *sys.argv[2:]]
    t0, rc = time.monotonic(), 0
    try:
        runpy.run_path(str(repo / script), run_name="__main__")
    except SystemExit as e:
        if isinstance(e.code, int):
            rc = e.code
        elif e.code is not None:
            print(e.code, file=sys.stderr)
            rc = 1
    except BaseException:
        traceback.print_exc()
        rc = 1
    print(f"PEAKRUN {script} runtime {time.monotonic() - t0:.1f} s peak {peak_mib():.0f} MiB", file=sys.stderr)
    sys.exit(rc)


if __name__ == "__main__":
    main()
