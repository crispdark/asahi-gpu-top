# asahi-gpu-top

Real-time GPU busy monitor for Apple Silicon on Asahi Linux.

## What it does

asahi-gpu-top tracks GPU activity on Apple M-series chips running
Asahi Linux by reading DRM scheduler trace events from the kernel
(`drm_sched_job_run` / `drm_sched_job_done`).

It shows:
- **GPU busy %** — percentage of time the GPU had at least one job
  running (equivalent to AMD's `gpu_busy_percent`)
- **jobs/s** — GPU job throughput per second
- **System power** and **fan speed** (whole-system metrics, not GPU-only)
- **Active GPU clients** — which processes currently have an open
  connection to the GPU

## Why this exists

The AGX driver on Asahi Linux does not yet expose hardware performance
counters, so true GPU utilization (shader occupancy, memory bandwidth,
etc.) is not available. GPU busy % is the closest meaningful metric
achievable from userspace today, and is the same metric used by AMD's
`gpu_busy_percent` sysfs entry.

Temperature and clock frequency are also not exposed by the current
`macsmc-hwmon` driver.

## Requirements

- Asahi Linux (tested on Fedora Asahi Remix, Apple M1 Pro)
- Python 3
- `rich` library: `sudo pip install rich --break-system-packages`
- Root access (required to read tracefs)

## Usage

```bash
sudo python3 asahi-gpu-top.py
```

**Keybindings:**
- `S` — toggle system processes (KDE/Wayland) in the client list
- `Q` — quit

## Limitations

- GPU busy % measures *how often* the GPU is working, not *how hard*
- System power and fan are whole-system metrics (CPU + GPU + display)
- GPU temperature and clock frequency are not available on Asahi Linux
  at this time

## Contributing

If you know how to expose AGX hardware counters or GPU temperature via
`macsmc-hwmon`, contributions are very welcome.

## License

MIT
