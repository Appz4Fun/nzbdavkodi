# CoreELEC and Linux tuning

NZB-DAV runs well out of the box. This page is an **advanced, optional**
reference for a specific demanding scenario: 4K Dolby Vision playback running
concurrently with heavy TMDBHelper cache-warming on a low-memory ARM device.

None of this is required to use NZB-DAV. It documents a real, tested
configuration for a **CoreELEC box on an Amlogic S922X** (6-core big.LITTLE,
4 GB RAM, Samsung T5 SSD over USB 3.0), which you can adapt if you push a similar
device hard.

!!! note "Device-specific"
    These values were tuned for one hardware profile. Treat them as a worked
    example, not universal settings. CoreELEC uses a read-only squashfs root, so
    persistent tuning lives under `/storage/` — applied via
    `/storage/.config/autostart.sh` and systemd units in
    `/storage/.config/system.d/`.

## What gets tuned, and why

| Area | Change | Why it helped |
|------|--------|---------------|
| **USB storage (UAS)** | Load a cross-compiled `uas.ko` and rebind the SSD from `usb-storage` to UAS, raising queue depth from 1 to 30 | Parallel SCSI queuing dropped write latency from ~10 ms to ~6 ms; SQLite checkpoints flush across NAND channels instead of serially |
| **SSD hints** | `echo 0 > /sys/block/sda/queue/rotational` | Stops HDD heuristics (anticipatory merging, oversized readahead) that add latency on an SSD hidden behind a USB bridge |
| **Dirty pages** | Absolute limits: `dirty_background_bytes=64 MB`, `dirty_bytes=256 MB`, 1 s writeback, 10 s expiry | The default 20% ratio allowed ~760 MB of dirty pages, causing write stalls; absolute byte limits keep the dirty set shallow so `fsync()` returns quickly |
| **Network (RPS)** | `rps_cpus=3f` across all 6 cores + a flow table | The `meson6-dwmac` NIC has a single RX queue; nearly all NET_RX softirqs landed on one core, capping throughput under 90+ concurrent connections. RPS spreads packets across all cores |
| **TCP** | `tcp_tw_reuse=1`, `tcp_fin_timeout=30`, ephemeral port range `1024–65535` | Hundreds of short-lived HTTP connections per second exhausted the default port range in TIME_WAIT. Existing 16 MB socket buffers were already fine |
| **Memory margin** | `vm.min_free_kbytes=32 MB` | Kodi's peak reaches ~3.8 GB during 4K DV; the default 16 MB margin left almost no room before emergency reclaim |
| **zram swap** | 2 GB lz4 compressed swap, priority 100 | The S922X has no disk swap; at peak load this turns OOM kills into manageable swap pressure (~700 MB physical for 2 GB swap) |
| **Page-cache warmup** | Read `Textures13.db` into cache at boot | Kodi queries the 545 MB texture DB for every on-screen image; pre-warming turns cold ~0.5 ms SSD reads into ~0.001 ms RAM hits |
| **Process priority** | Warmup services set to `Nice=19`, best-effort I/O priority 7 | Keeps background warmup from competing with Kodi's I/O, without SIGSTOP throttling |
| **CPU governor** | `performance` on all cores (CoreELEC default) | Eliminates frequency-scaling latency for HDR tone mapping and UI bursts; power draw is irrelevant on a plugged-in device |

## Verifying

```bash
cat /sys/block/sda/device/queue_depth        # expect 30 (UAS active)
cat /sys/class/net/eth0/queues/rx-0/rps_cpus  # expect 3f (RPS active)
lsmod | grep uas                              # expect uas loaded
```

## How this relates to NZB-DAV

NZB-DAV's own read-ahead buffer and stall-wait settings (see
[Stream proxy](../how-it-works/stream-proxy.md)) address playback smoothness at
the application layer. The tuning above addresses the same goal at the OS layer —
keeping storage and network responsive while Kodi and any warmup services compete
for a small pool of RAM and a single-queue NIC. On a roomier device (more RAM, a
multi-queue NIC, native SATA), most of this is unnecessary.
