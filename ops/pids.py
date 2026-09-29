"""C:\\projects\\market-maker\\PIDS.txt: which python processes belong to market-maker, so other agents on this
machine don't kill them by mistake. Each writer (supervisor, daily job) owns one block and replaces only that."""
import json, subprocess
from pathlib import Path

HEADER = ("market-maker 24/7 recorder. DO NOT KILL any process listed here. "
          "Stop gracefully: create C:\\projects\\market-maker\\STOP\n")


def describe(pids):
    """PID, parent, start time and full CommandLine for `pids` and their direct children."""
    ids = " or ".join(f"ProcessId={p} or ParentProcessId={p}" for p in pids)
    ps = (f"Get-CimInstance Win32_Process -Filter '{ids}' | Select-Object ProcessId,ParentProcessId,"
          "@{N='Started';E={$_.CreationDate.ToString('yyyy-MM-dd HH:mm:ss')}},CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           timeout=60, creationflags=0x08000000)
        rows = json.loads(r.stdout or "[]")
    except Exception as e:  # PIDS.txt is advisory; never let it break the caller
        return [f"(could not describe {pids}: {e!r})"]
    rows = [rows] if isinstance(rows, dict) else rows
    rows = [x for x in rows if "Get-CimInstance Win32_Process -Filter" not in (x["CommandLine"] or "")]  # this query
    return [f"pid={x['ProcessId']} parent={x['ParentProcessId']} started={x['Started']} cmd={x['CommandLine']}"
            for x in sorted(rows, key=lambda x: x["ProcessId"])]


def update(repo, name, lines):
    """Replace this writer's block in PIDS.txt (lines=None removes it)."""
    f = Path(repo) / "PIDS.txt"
    blocks, cur = {}, None
    if f.exists():
        for line in f.read_text(errors="replace").splitlines():
            if line.startswith("## "):
                cur = line[3:].strip()
                blocks[cur] = []
            elif cur:
                blocks[cur].append(line)
    if lines is None:
        blocks.pop(name, None)
    else:
        blocks[name] = list(lines)
    f.write_text(HEADER + "".join(f"\n## {k}\n" + "".join(l + "\n" for l in v) for k, v in blocks.items()))
