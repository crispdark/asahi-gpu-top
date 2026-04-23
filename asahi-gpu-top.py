#!/usr/bin/env python3
"""
asahi-gpu-top — GPU busy monitor for Asahi Linux on Apple Silicon
Computes GPU busy percentage by tracking drm_sched_job_run/done via tracefs.

GPU busy % = time the GPU had at least one job running / total window time
This is equivalent to AMD's gpu_busy_percent — a standard Linux GPU metric.
It measures *how often* the GPU is working, not *how hard* it is working.

Requires sudo.
"""

import os
import sys
import re
import time
import threading
import subprocess
from collections import deque

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    print("Install rich first: sudo pip install rich --break-system-packages")
    sys.exit(1)

# ── tracefs paths ────────────────────────────────────────────────────────────
TRACEFS     = "/sys/kernel/debug/tracing"
TRACE_PIPE  = f"{TRACEFS}/trace_pipe"
TRACING_ON  = f"{TRACEFS}/tracing_on"
EVENT_RUN   = f"{TRACEFS}/events/gpu_scheduler/drm_sched_job_run/enable"
EVENT_DONE  = f"{TRACEFS}/events/gpu_scheduler/drm_sched_job_done/enable"
HWMON       = "/sys/class/hwmon/hwmon1"
DRI_CLIENTS = "/sys/kernel/debug/dri/1/clients"

WINDOW_SEC     = 0.25   # busy% calculation window
SMOOTH_SAMPLES = 8      # moving average over N samples

# ── shared state ─────────────────────────────────────────────────────────────
lock          = threading.Lock()
job_run       = {}
job_busy      = deque()
util_history  = deque(maxlen=SMOOTH_SAMPLES)
gpu_busy_pct  = 0.0
jobs_per_sec  = 0.0
running       = True

RE_RUN  = re.compile(r'(\d+\.\d+): drm_sched_job_run:.*fence=\d+:(\d+)')
RE_DONE = re.compile(r'(\d+\.\d+): drm_sched_job_done: fence=\d+:(\d+)')

def write_file(path, val):
    with open(path, "w") as f:
        f.write(val)

def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None

# ── trace reader thread ───────────────────────────────────────────────────────
def trace_reader():
    global gpu_busy_pct, jobs_per_sec
    with open(TRACE_PIPE, "r", buffering=1) as pipe:
        for line in pipe:
            if not running:
                break

            m = RE_RUN.search(line)
            if m:
                ts, seqno = float(m.group(1)), m.group(2)
                with lock:
                    job_run[seqno] = ts
                continue

            m = RE_DONE.search(line)
            if m:
                ts, seqno = float(m.group(1)), m.group(2)
                with lock:
                    t_start = job_run.pop(seqno, None)
                    if t_start is not None:
                        duration = ts - t_start
                        if 0 < duration < 1.0:
                            job_busy.append((t_start, ts))

                    cutoff = ts - WINDOW_SEC
                    while job_busy and job_busy[0][1] < cutoff:
                        job_busy.popleft()

                    win_start = ts - WINDOW_SEC
                    busy_time = sum(
                        min(e, ts) - max(s, win_start)
                        for s, e in job_busy
                        if min(e, ts) > max(s, win_start)
                    )
                    raw = min(100.0, (busy_time / WINDOW_SEC) * 100.0)
                    util_history.append(raw)
                    gpu_busy_pct = sum(util_history) / len(util_history)
                    jobs_per_sec = len(job_busy) / WINDOW_SEC

# ── sensors ──────────────────────────────────────────────────────────────────
def get_power():
    v = read_file(f"{HWMON}/power1_input")
    return float(v) / 1_000_000 if v else None

def get_fan():
    v = read_file(f"{HWMON}/fan1_input")
    return int(v) if v else None

def get_fan_max():
    v = read_file(f"{HWMON}/fan1_max")
    return int(v) if v else 4499

def get_gpu_clients():
    try:
        r = subprocess.run(["cat", DRI_CLIENTS],
                           capture_output=True, text=True, timeout=2)
        lines = r.stdout.strip().splitlines()[1:]
        seen = {}
        for line in lines:
            parts = line.split()
            if len(parts) < 2:
                continue
            key = (parts[0], parts[1])
            seen[key] = seen.get(key, 0) + 1
        return [(n, p, c) for (n, p), c in seen.items()]
    except Exception:
        return []

# ── UI ────────────────────────────────────────────────────────────────────────
SYSTEM_PROCS = {
    "kwin_wayland","plasmashell","Xwayland","kded6","ksmserver",
    "plasma-keyboard","ksecretd","kaccess","xdg-desktop-por",
    "polkit-kde-auth","kdeconnectd","xwaylandvideobr","DiscoverNotifie",
    "kalendarac","akonadi_control","akonadi_followu","akonadi_maildis",
    "akonadi_mailmer","akonadi_newmail","akonadi_migrati","akonadi_imap_re",
    "akonadi_unified","akonadi_archive","akonadi_google_","akonadi_mailfil",
    "akonadi_sendlat","kwalletd6","plasma-browser-","plasma-systemmo",
    "safeeyes","baloorunner",
}

def bar(value, max_v, width=28, color="cyan"):
    filled = min(int((value / max_v) * width) if max_v > 0 else 0, width)
    return Text("█" * filled + "░" * (width - filled), style=color)

def busy_color(pct):
    return "green" if pct < 30 else "yellow" if pct < 70 else "red"

def build_ui(show_system):
    with lock:
        busy = gpu_busy_pct
        jps  = jobs_per_sec

    power   = get_power()
    fan     = get_fan()
    fan_max = get_fan_max()
    clients = get_gpu_clients()
    filtered = clients if show_system else [
        (n, p, c) for n, p, c in clients if n not in SYSTEM_PROCS
    ]

    # header
    h = Text()
    h.append("◆ ASAHI GPU TOP", style="bold white")
    h.append("  Apple M1 Pro · Fedora Asahi Remix", style="dim white")

    # metrics grid
    g = Table.grid(padding=(0, 2))
    g.add_column(justify="right", style="dim white", min_width=12)
    g.add_column(min_width=12)
    g.add_column()

    bc = busy_color(busy)
    # Busy% — clearly labeled with explanation
    g.add_row(
        "GPU busy",
        Text(f"{busy:5.1f} %", style=f"bold {bc}"),
        bar(busy, 100, color=bc),
    )
    g.add_row(
        "",
        Text("% of time GPU had ≥1 job running", style="dim white"),
        Text(""),
    )
    g.add_row(
        "jobs/s",
        Text(f"{jps:5.1f}", style="dim cyan"),
        Text(""),
    )

    if power is not None:
        pc = "green" if power < 15 else "yellow" if power < 30 else "red"
        g.add_row(
            "sys power †",
            Text(f"{power:5.1f} W", style=f"bold {pc}"),
            bar(power, 60, color=pc),
        )

    if fan is not None:
        fc = "green" if fan/fan_max < 0.5 else "yellow" if fan/fan_max < 0.8 else "red"
        g.add_row(
            "sys fan †",
            Text(f"{fan:5d} RPM", style=f"bold {fc}"),
            bar(fan, fan_max, color=fc),
        )

    top = Panel(g, title=h, border_style="bright_black", padding=(1, 2))

    # client table
    t = Table(box=box.SIMPLE, show_header=True,
              header_style="bold dim white", padding=(0, 1), expand=True)
    t.add_column("PROCESS",  style="bright_white", min_width=22)
    t.add_column("PID",      style="cyan",         justify="right")
    t.add_column("CONN.",    style="dim cyan",      justify="center")

    for name, pid, conns in sorted(filtered, key=lambda x: x[0]):
        t.add_row(name, pid, str(conns))
    if not filtered:
        t.add_row(Text("no active clients", style="dim italic"), "", "")

    bottom = Panel(
        t,
        title=(f"[bold white]GPU CLIENTS[/bold white]  "
               f"[dim]{len(filtered)} shown / {len(clients)} total[/dim]"),
        border_style="bright_black",
        padding=(0, 1),
    )

    hint = Text.from_markup(
        "[dim]† whole-system metrics (CPU+GPU+display)  │  "
        "[S] toggle system procs  │  [Q] quit[/dim]"
    )

    return Group(top, bottom, hint)

# ── tracefs setup/teardown ───────────────────────────────────────────────────
def setup_tracing():
    write_file(f"{TRACEFS}/trace", "")
    write_file(EVENT_RUN,  "1")
    write_file(EVENT_DONE, "1")
    write_file(TRACING_ON, "1")

def teardown_tracing():
    try:
        write_file(EVENT_RUN,  "0")
        write_file(EVENT_DONE, "0")
        write_file(TRACING_ON, "0")
        write_file(f"{TRACEFS}/trace", "")
    except Exception:
        pass

# ── main ─────────────────────────────────────────────────────────────────────
def main():
    global running

    if os.geteuid() != 0:
        print("Root required: sudo python3 asahi-gpu-top.py")
        sys.exit(1)

    setup_tracing()
    threading.Thread(target=trace_reader, daemon=True).start()

    import termios, tty, select
    show_system = False
    console = Console()
    old = termios.tcgetattr(sys.stdin)

    try:
        tty.setcbreak(sys.stdin.fileno())
        with Live(build_ui(show_system), refresh_per_second=4,
                  screen=True, console=console) as live:
            while True:
                dr, _, _ = select.select([sys.stdin], [], [], 0.1)
                if dr:
                    key = sys.stdin.read(1).lower()
                    if key == "q":
                        break
                    elif key == "s":
                        show_system = not show_system
                live.update(build_ui(show_system))

    except KeyboardInterrupt:
        pass
    finally:
        running = False
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        teardown_tracing()

    console.print(
        "\n[bold white]Bye bye![/bold white] "
        "[dim]Thanks for using asahi-gpu-top — "
        "GPU busy % is the best we can do until AGX exposes hardware counters.[/dim]\n"
    )

if __name__ == "__main__":
    main()
