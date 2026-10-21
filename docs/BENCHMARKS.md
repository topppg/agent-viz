# Local collector baseline

These are small local measurements, not a claim about every computer or a loaded dashboard.
They measure the Python collector, not the browser. Native account discovery and AI summaries
are disabled. No real sessions, credentials or model APIs are used.

## Reproduce

From a source checkout, with dependencies already installed:

```bash
uv sync --locked
uv run python scripts/benchmark_startup.py --samples 7
```

The helper supports macOS and Linux (`ps` is required). It prints JSON containing every sample,
the median and range, and generic machine details. It never prints a hostname or local paths.
Installation and the outer `uv` command are outside the timed region.

Each sample starts a fresh Python process with:

```text
spiritboard serve --demo --demo-delay 0 --no-discovery --no-open --port 0 --data-dir <fresh temporary directory>
```

Only `PATH`, `SYSTEMROOT` and `LANG`, if present, are passed from the caller's environment.
The synthetic demo disables provider connections, while `--no-discovery` disables native profile
scanning. Each owned process is stopped and its temporary data is removed, including on failure.
Startup waits have a 10-second deadline by default; shutdown and `ps` calls are also bounded.

## What is measured

- **Health-ready time:** elapsed wall time immediately before creating the Python process until
  `/api/health` returns HTTP 200 with the matching instance ID. This includes Python startup,
  imports, initial local database creation and HTTP startup. Readiness is polled every 10 ms;
  it is not the time to render a page or finish demo ingestion.
- **Observed collector RSS:** wait one second after health readiness, then read the process's
  current resident memory with `ps -o rss= -p PID` five times, 100 ms apart. Each sample records
  the median of those five readings, converted from KiB to MiB. The reported overall median
  and range describe the seven per-launch medians. This is **not peak memory**, Python heap
  size, or browser memory, and does not establish a long-running steady-state value.

Each process and data directory is new, but operating-system file caches are not flushed.
This is a fresh-process/warm-filesystem baseline, not a cold-machine benchmark. Real discovery,
large histories, concurrent sessions, summaries, browser rendering, other machine activity and
different hardware will change the result. The synthetic demo emits only five invented runs.

## Recorded sample: October 21, 2026

Code baseline: `ca169f0` (Spiritboard 0.1.0). Machine: Apple M4 Pro, arm64,
12 logical CPUs; Darwin 24.6.0; CPython 3.13.11. Default settings above, seven sequential launches.

| Metric | Median | Per-launch range |
| --- | ---: | ---: |
| Process-to-health readiness | 75.64 ms | 72.49–89.69 ms |
| Observed collector RSS | 33.94 MiB | 32.47–37.16 MiB |

| Launch | Health ready (ms) | Observed RSS (MiB) |
| --- | ---: | ---: |
| 1 | 89.69 | 37.16 |
| 2 | 75.64 | 32.75 |
| 3 | 74.09 | 32.47 |
| 4 | 84.79 | 35.09 |
| 5 | 72.49 | 33.84 |
| 6 | 88.83 | 34.38 |
| 7 | 72.89 | 33.94 |

Treat these numbers as a reproducible reference point, not a promise for native-profile workloads.
