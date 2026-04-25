# Linux Kernel & System Tuning — CoreELEC (Amlogic S922X)

Configuration for a CoreELEC media center running Kodi with 4K Dolby Vision
playback concurrent with TMDBHelper cache warming services. Target hardware:
Amlogic S922X (6-core big.LITTLE), 4 GB RAM, Samsung T5 SSD on USB 3.0.

All tuning is applied via `/storage/.config/autostart.sh` (sysctls/sysfs) and
systemd units in `/storage/.config/system.d/`. CoreELEC uses a read-only
squashfs root, so persistent files must live under `/storage/`.

---

## 1. USB Storage — UAS Driver (queue_depth 1 → 30)

**Problem:** The kernel's `usb-storage` driver uses Bulk-Only Transport (BOT)
with `queue_depth=1` — one SCSI command at a time. The Samsung T5 supports USB
Attached SCSI (UAS) with `queue_depth=30`, but CoreELEC's kernel 4.9.269 was
built with `CONFIG_USB_UAS=n`.

**Solution:** Cross-compiled `uas.ko` from the kernel source and created a
boot-time rebind service. The `usb-storage` driver is built-in and claims the
device during kernel init; the rebind script loads UAS, unbinds from
usb-storage, and lets UAS claim the device with parallel SCSI command queuing.

**Files:**
- `/storage/uas.ko` — compiled UAS module (vermagic must match kernel exactly)
- `/storage/.config/uas-rebind.sh` — load module, unbind, rebind, fallback
- `/storage/.config/system.d/uas-rebind.service` — runs at `sysinit.target`

**Impact:** Write latency 10 ms → 6 ms. SQLite checkpoint flushes run in
parallel across SSD NAND channels instead of serialized.

**Verification:**
```bash
cat /sys/block/sda/device/queue_depth   # expect 30
cat /sys/block/sda/queue/nr_requests    # expect 30
lsmod | grep uas                        # expect loaded, refcount 1
```

---

## 2. SSD I/O Hints

```bash
# Tell kernel this is an SSD (USB bridge hides TRIM/rotational info)
echo 0 > /sys/block/sda/queue/rotational
```

Eliminates HDD heuristics: anticipatory I/O merging, seek-adjacent reordering,
and oversized readahead that add latency on SSDs.

---

## 3. VM Dirty Page Tuning

```bash
sysctl -w vm.dirty_background_bytes=67108864   # 64 MB: start background flush
sysctl -w vm.dirty_bytes=268435456             # 256 MB: hard sync stall limit
sysctl -w vm.dirty_expire_centisecs=1000       # 10s max dirty page age
sysctl -w vm.dirty_writeback_centisecs=100     # 1s writeback thread wakeup
```

**Why:** Default percentage-based `dirty_ratio=20` allows 760 MB of dirty pages
on a 3.8 GB system — during heavy image downloads this caused write stalls when
the kernel hit the ratio and blocked all writers. Absolute byte limits give
predictable behavior. The aggressive writeback (1s interval, 10s expiry) keeps
the dirty page count shallow so SQLite `fsync()` completes quickly.

---

## 4. Receive Packet Steering (RPS)

```bash
echo 3f > /sys/class/net/eth0/queues/rx-0/rps_cpus
echo 4096 > /sys/class/net/eth0/queues/rx-0/rps_flow_cnt
sysctl -w net.core.rps_sock_flow_entries=4096
```

**Why:** The Amlogic ethernet driver (`meson6-dwmac`) has a single RX queue
with no hardware RSS. Under audit, 99.99% of NET_RX softirqs landed on CPU2
(3.76M vs 4-11K on other cores). With 90+ concurrent HTTP connections for TMDB
API and CDN image downloads, single-core packet processing was a throughput
ceiling.

RPS distributes incoming packets across all 6 cores by hashing each flow
(source/dest IP+port). The `3f` bitmask enables all 6 cores. Each flow is
pinned to a consistent core via the flow table to avoid cache-line bouncing.

---

## 5. TCP Stack Tuning

```bash
sysctl -w net.ipv4.tcp_tw_reuse=1              # recycle TIME_WAIT for outbound
sysctl -w net.ipv4.tcp_fin_timeout=30           # 30s half-close (default 60)
sysctl -w net.ipv4.ip_local_port_range="1024 65535"  # 64K ports (default 28K)
```

**Why:** The warmup services make hundreds of short-lived HTTP connections per
second. Each closed connection holds an ephemeral port in TIME_WAIT for
`tcp_fin_timeout` seconds. At 90+ conn/s with 60s timeout, that's 5,400 ports
in TIME_WAIT — 19% of the default 28K range. `tcp_tw_reuse` allows outbound
connections to reuse TIME_WAIT sockets, and the expanded port range provides
headroom.

The existing TCP buffer sizes (`rmem_max=16 MB`, `wmem_max=16 MB`) are already
well-tuned for high-throughput streaming and don't need changes.

---

## 6. Memory Safety Margin

```bash
sysctl -w vm.min_free_kbytes=32768   # 32 MB (default was 16 MB)
```

**Why:** Kodi's `VmPeak` reaches 3.8 GB during 4K Dolby Vision (64% of RAM for
the process alone, plus 610 MB CMA for the hardware video decoder). With warmup
services adding ~800 MB, the working set approaches physical RAM. The default
16 MB `min_free_kbytes` left almost no margin before the kernel entered
emergency direct reclaim. 32 MB gives the allocator breathing room.

---

## 7. Compressed Swap (zram)

**File:** `/storage/.config/system.d/zram.service`

```
2 GB zram device, lz4 compression, swap priority 100
```

**Why:** The S922X has no disk swap partition. Under peak load (4K DV + warmup
at c=100), Kodi alone uses 2.4 GB RSS. Without swap, the OOM killer would
terminate warmup or Kodi. zram provides 2 GB of compressed swap in RAM — at
lz4's typical 2.9:1 ratio, 2 GB of swap only costs ~700 MB of physical RAM.
This turns OOM scenarios into manageable swap pressure.

---

## 8. Page Cache Warmup

```bash
cat /var/media/CACHE_DRIVE/tmdb/Textures13.db > /dev/null 2>&1 &
```

**Why:** Kodi's texture loader queries Textures13.db (545 MB, 2.2M rows) for
every image on screen. After a reboot, the DB is cold — each `SELECT` reads
B-tree pages from SSD at ~0.5 ms each. Reading the entire DB into page cache at
boot makes those queries hit RAM (~0.001 ms) — a 500x per-lookup speedup. The
`&` backgrounds it so Kodi startup isn't delayed.

---

## 9. Process Priorities

Set via systemd unit files for the warmup services:

```ini
Nice=19                  # lowest CPU priority
IOSchedulingClass=2      # best-effort
IOSchedulingPriority=7   # lowest I/O priority within best-effort
```

Combined with UAS `queue_depth=30`, Kodi's I/O requests are served in parallel
with warmup writes. The kernel's CFS scheduler and I/O priority ensure Kodi
always gets resources first. No SIGSTOP throttling needed — the scheduler
handles contention naturally.

---

## 10. CPU Governor

CoreELEC ships with `governor=performance` on all 6 cores (1.8 GHz LITTLE,
2.208 GHz big). This is correct for a media center — eliminates frequency
scaling latency when Kodi needs burst compute for HDR tone mapping or UI
rendering. Power consumption is irrelevant for a plugged-in device.

---

## Quick Reference — All Files

| File | Purpose |
|------|---------|
| `/storage/.config/autostart.sh` | sysctls, sysfs, page cache warmup |
| `/storage/.config/uas-rebind.sh` | UAS module load + USB rebind |
| `/storage/.config/system.d/uas-rebind.service` | runs rebind at sysinit |
| `/storage/.config/system.d/zram.service` | 2 GB lz4 compressed swap |
| `/storage/.config/system.d/tmdbhelper-warmup-rs.service` | metadata service |
| `/storage/.config/system.d/tmdbhelper-warmup-images.service` | image service |
| `/storage/uas.ko` | compiled UAS kernel module |

## Verification Commands

```bash
# UAS active
cat /sys/block/sda/device/queue_depth        # 30
basename $(readlink /sys/bus/usb/drivers/uas/2-1:1.0/driver)  # uas

# Filesystem clean
tune2fs -l /dev/sda1 | grep "Filesystem state"  # clean

# Textures DB version
sqlite3 /var/media/CACHE_DRIVE/tmdb/Textures13.db "SELECT idVersion FROM version"  # 13

# RPS active
cat /sys/class/net/eth0/queues/rx-0/rps_cpus  # 3f

# Services running
systemctl is-active tmdbhelper-warmup-rs tmdbhelper-warmup-images  # active active
```
