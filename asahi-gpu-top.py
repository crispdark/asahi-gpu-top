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
import glob
import time
import threading
from collections import deque

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    print("Install rich first: sudo dnf install python3-rich  (or: pipx install rich)")
    sys.exit(1)

# ── tracefs paths ────────────────────────────────────────────────────────────
TRACEFS = "/sys/kernel/debug/tracing"
TRACE_PIPE = f"{TRACEFS}/trace_pipe"
TRACING_ON = f"{TRACEFS}/tracing_on"
EVENT_RUN = f"{TRACEFS}/events/gpu_scheduler/drm_sched_job_run/enable"
EVENT_DONE = f"{TRACEFS}/events/gpu_scheduler/drm_sched_job_done/enable"

# ── tunables ─────────────────────────────────────────────────────────────────
WINDOW_SEC = 0.25          # busy% calculation window
SMOOTH_SAMPLES = 8         # moving average over N samples
TICK_SEC = 0.1             # periodic recompute interval (drives idle decay)
MAX_JOB_DURATION = 10.0    # discard obvious trace glitches (seconds)
STALE_JOB_SEC = 5.0        # drop unmatched job_run entries older than this
POWER_MAX_W = 60           # UI bar scale for system power
POWER_WARN_W = 15
POWER_CRIT_W = 30
FAN_WARN_RATIO = 0.5
FAN_CRIT_RATIO = 0.8

# ── dynamic hwmon / DRI discovery ────────────────────────────────────────────
def find_hwmon(name_match):
    """Return the first /sys/class/hwmon/hwmonN whose 'name' contains name_match."""
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(f"{hw}/name") as f:
                if name_match in f.read().strip():
                    return hw
        except OSError:
            continue
    return None


def find_dri_clients(driver_match="asahi"):
    """Return /sys/kernel/debug/dri/N/clients for the first matching DRM driver."""
    for card in glob.glob("/sys/kernel/debug/dri/*"):
        base = os.path.basename(card)
        if not base.isdigit():
            continue
        try:
            with open(f"{card}/name") as f:
                if driver_match in f.read().strip().lower():
                    return f"{card}/clients"
        except OSError:
            continue
    return None


HWMON = find_hwmon("macsmc")        # Apple SMC hwmon; may be None on unusual setups
DRI_CLIENTS = find_dri_clients()    # AGX DRM clients file

# ── shared state ─────────────────────────────────────────────────────────────
lock = threading.Lock()
stop_event = threading.Event()
job_run = {}
job_busy = deque()
util_history = deque(maxlen=SMOOTH_SAMPLES)
gpu_busy_pct = 0.0
jobs_per_sec = 0.0

RE_RUN = re.compile(r'(\d+\.\d+): drm_sched_job_run:.*fence=\d+:(\d+)')
RE_DONE = re.compile(r'(\d+\.\d+): drm_sched_job_done: fence=\d+:(\d+)')


def write_file(path, val):
    with open(path, "w") as f:
        f.write(val)


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# ── busy% math ───────────────────────────────────────────────────────────────
def _union_busy(intervals, win_start, win_end):
    """
    Length of the union of [s,e] intervals clipped to [win_start, win_end].
    Using the union (not the sum) is required for GPU busy %, defined as
    'time with at least one job running'. Summing double-counts concurrent jobs.
    """
    clipped = [
        (max(s, win_start), min(e, win_end))
        for s, e in intervals
        if min(e, win_end) > max(s, win_start)
    ]
    if not clipped:
        return 0.0
    clipped.sort()
    total = 0.0
    cur_s, cur_e = clipped[0]
    for s, e in clipped[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return total


def recompute(now_ts):
    """Recompute rolling busy% and jobs/s based on current deque state."""
    global gpu_busy_pct, jobs_per_sec
    cutoff = now_ts - WINDOW_SEC
    with lock:
        while job_busy and job_busy[0][1] < cutoff:
            job_busy.popleft()
        # evict stale unmatched job_run entries to bound memory if DONE events are lost
        stale = [k for k, t in job_run.items() if t < now_ts - STALE_JOB_SEC]
        for k in stale:
            job_run.pop(k, None)
        busy = _union_busy(list(job_busy), cutoff, now_ts)
        raw = min(100.0, (busy / WINDOW_SEC) * 100.0)
        util_history.append(raw)
        gpu_busy_pct = sum(util_history) / len(util_history)
        jobs_per_sec = len(job_busy) / WINDOW_SEC


# ── trace reader thread ──────────────────────────────────────────────────────
def trace_reader():
    """Parse trace_pipe lines and push completed jobs into the busy deque."""
    with open(TRACE_PIPE, "r", buffering=1) as pipe:
        for line in pipe:
            if stop_event.is_set():
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
                    if 0 < duration < MAX_JOB_DURATION:
                        with lock:
                            job_busy.append((t_start, ts))


def ticker_thread():
    """Drive periodic recompute so busy% decays to zero when GPU goes idle."""
    while not stop_event.is_set():
        recompute(time.monotonic())
        stop_event.wait(TICK_SEC)


# ── sensors ──────────────────────────────────────────────────────────────────
def get_power():
    if not HWMON:
        return None
    v = read_file(f"{HWMON}/power1_input")
    return float(v) / 1_000_000 if v else None


def get_fan():
    if not HWMON:
        return None
    v = read_file(f"{HWMON}/fan1_input")
    return int(v) if v else None


def get_fan_max():
    if not HWMON:
        return None
    v = read_file(f"{HWMON}/fan1_max")
    return int(v) if v else None


def get_gpu_clients():
    if not DRI_CLIENTS:
        return []
    content = read_file(DRI_CLIENTS)
    if not content:
        return []
    lines = content.splitlines()[1:]  # skip header
    seen = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        key = (parts[0], parts[1])
        seen[key] = seen.get(key, 0) + 1
    return [(n, p, c) for (n, p), c in seen.items()]


# ── UI ───────────────────────────────────────────────────────────────────────
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
        jps = jobs_per_sec

    power = get_power()
    fan = get_fan()
    fan_max = get_fan_max()
    clients = get_gpu_clients()
    filtered = clients if show_system else [
        (n, p, c) for n, p, c in clients if n not in SYSTEM_PROCS
    ]

    # header
    h = Text()
    h.append("◆ ASAHI GPU TOP", style="bold white")
    h.append("  Asahi Linux · Apple Silicon", style="dim white")

    # metrics grid
    g = Table.grid(padding=(0, 2))
    g.add_column(justify="right", style="dim white", min_width=12)
    g.add_column(min_width=12)
    g.add_column()

    bc = busy_color(busy)
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
        pc = "green" if power < POWER_WARN_W else "yellow" if power < POWER_CRIT_W else "red"
        g.add_row(
            "sys power †",
            Text(f"{power:5.1f} W", style=f"bold {pc}"),
            bar(power, POWER_MAX_W, color=pc),
        )
    if fan is not None:
        if fan_max:
            ratio = fan / fan_max
            fc = "green" if ratio < FAN_WARN_RATIO else "yellow" if ratio < FAN_CRIT_RATIO else "red"
            fan_bar = bar(fan, fan_max, color=fc)
        else:
            fc = "dim cyan"
            fan_bar = Text("")
        g.add_row(
            "sys fan †",
            Text(f"{fan:5d} RPM", style=f"bold {fc}"),
            fan_bar,
        )

    top = Panel(g, title=h, border_style="bright_black", padding=(1, 2))

    # client table
    t = Table(box=box.SIMPLE, show_header=True,
              header_style="bold dim white", padding=(0, 1), expand=True)
    t.add_column("PROCESS", style="bright_white", min_width=22)
    t.add_column("PID", style="cyan", justify="right")
    t.add_column("CONN.", style="dim cyan", justify="center")

    for name, pid, conns in sorted(filtered, key=lambda x: x[0]):
        t.add_row(name, pid, str(conns))
    if not filtered:
        t.add_row(Text("no active clients", style="dim italic"), "", "")

    bottom = Panel(
        t,
        title=(f"[bold white]GPU CLIENTS[/bold white] "
               f"[dim]{len(filtered)} shown / {len(clients)} total[/dim]"),
        border_style="bright_black",
        padding=(0, 1),
    )
    hint = Text.from_markup(
        "[dim]† whole-system metrics (CPU+GPU+display) │ "
        "[S] toggle system procs │ [Q] quit[/dim]"
    )

    return Group(top, bottom, hint)


# ── tracefs setup/teardown ───────────────────────────────────────────────────
_prev_state = {}


def check_tracing_available():
    """Return the first missing path, or None if everything is in place."""
    for p in (EVENT_RUN, EVENT_DONE, TRACE_PIPE, TRACING_ON):
        if not os.path.exists(p):
            return p
    return None


def setup_tracing():
    # save previous enable state so we can restore it on exit
    for p in (EVENT_RUN, EVENT_DONE, TRACING_ON):
        _prev_state[p] = read_file(p) or "0"
    write_file(f"{TRACEFS}/trace", "")
    write_file(EVENT_RUN, "1")
    write_file(EVENT_DONE, "1")
    write_file(TRACING_ON, "1")


def teardown_tracing():
    for p in (EVENT_RUN, EVENT_DONE, TRACING_ON):
        try:
            write_file(p, _prev_state.get(p, "0"))
        except OSError:
            pass


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    if os.geteuid() != 0:
        print("Root required: sudo python3 asahi-gpu-top.py")
        sys.exit(1)

    missing = check_tracing_available()
    if missing:
        print(f"DRM scheduler tracing not available (missing: {missing}).")
        print("Kernel needs CONFIG_DRM_SCHED + tracefs mounted at /sys/kernel/debug/tracing.")
        sys.exit(1)

    if DRI_CLIENTS is None:
        print("No Asahi DRM device found under /sys/kernel/debug/dri/*; "
              "client list will be empty.")

    setup_tracing()
    threading.Thread(target=trace_reader, daemon=True).start()
    threading.Thread(target=ticker_thread, daemon=True).start()

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
        stop_event.set()
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        teardown_tracing()
        console.print(
            "\n[bold white]Bye bye![/bold white] "
            "[dim]Thanks for using asahi-gpu-top — "
            "GPU busy % is the best we can do until AGX exposes hardware counters.[/dim]\n"
        )


if __name__ == "__main__":
    main()
