#!/usr/bin/env python3
"""TRIM free blocks on an ext4 USB SSD via sg_unmap (SCSI UNMAP).

Works around kernel 4.9 not exposing TRIM for USB-attached SSDs.
Reads the ext4 block group descriptors to find free block ranges,
then sends SCSI UNMAP commands via sg_unmap from sg3_utils.

The Samsung T5's USB bridge supports UNMAP (LBPU=1, max_unmap_lba=4194240)
but the kernel's USB storage driver skips VPD page queries on kernel 4.9,
so the block layer never learns about it.
"""

import os
import re
import struct
import subprocess
import syslog
import time

DEV = "/dev/sda"
PART = "/dev/sda1"
PART_START_SECTORS = int(open("/sys/block/sda/sda1/start").read().strip())
SECTORS_PER_BLOCK = 8  # 4096-byte fs blocks / 512-byte sectors
MAX_UNMAP_LBA = 4194240  # from VPD page 0xB0
SG_UNMAP = "/storage/.opt/bin/sg_unmap"


def get_fs_params():
    info = subprocess.check_output(["tune2fs", "-l", PART], text=True, stderr=subprocess.DEVNULL)
    block_size = int(re.search(r"Block size:\s+(\d+)", info).group(1))
    blocks_per_group = int(re.search(r"Blocks per group:\s+(\d+)", info).group(1))
    block_count = int(re.search(r"Block count:\s+(\d+)", info).group(1))
    free_blocks = int(re.search(r"Free blocks:\s+(\d+)", info).group(1))
    first_data_block = int(re.search(r"First block:\s+(\d+)", info).group(1))
    return block_size, blocks_per_group, block_count, free_blocks, first_data_block


def read_block_bitmap(fd, bitmap_block, block_size, blocks_in_group):
    """Read one block group's bitmap and yield (start, count) free ranges."""
    offset = bitmap_block * block_size
    fd.seek(offset)
    bitmap = fd.read(block_size)

    pos = 0
    run_start = None

    for byte_idx in range(blocks_in_group // 8):
        b = bitmap[byte_idx]
        for bit in range(8):
            block_idx = byte_idx * 8 + bit
            if block_idx >= blocks_in_group:
                break
            allocated = (b >> bit) & 1
            if not allocated:
                if run_start is None:
                    run_start = block_idx
            else:
                if run_start is not None:
                    yield (run_start, block_idx - run_start)
                    run_start = None

    if run_start is not None:
        yield (run_start, blocks_in_group - run_start)


def read_group_descriptors(fd, block_size, num_groups, first_data_block):
    """Read block group descriptor table, return list of bitmap block numbers."""
    gdt_block = first_data_block + 1
    fd.seek(gdt_block * block_size)

    desc_size = 32  # standard ext4 32-byte descriptors
    bitmaps = []
    for _ in range(num_groups):
        desc = fd.read(desc_size)
        if len(desc) < desc_size:
            break
        bitmap_lo = struct.unpack_from("<I", desc, 0)[0]
        bitmaps.append(bitmap_lo)
    return bitmaps


def coalesce_ranges(ranges):
    """Merge adjacent LBA ranges to minimize sg_unmap calls."""
    if not ranges:
        return []
    sorted_r = sorted(ranges)
    merged = [sorted_r[0]]
    for start, count in sorted_r[1:]:
        prev_start, prev_count = merged[-1]
        if start == prev_start + prev_count:
            merged[-1] = (prev_start, prev_count + count)
        else:
            merged.append((start, count))
    return merged


def trim_ranges(ranges, total_sectors):
    """Send TRIM via sg_unmap. Logs progress with ETA every 60 seconds."""
    trimmed = 0
    errors = 0
    sectors_done = 0
    t0 = time.time()
    last_log = t0

    for i, (lba_start, lba_count) in enumerate(ranges):
        range_sectors = lba_count
        while lba_count > 0:
            chunk = min(lba_count, MAX_UNMAP_LBA)
            cmd = [SG_UNMAP, f"--lba={lba_start}", f"--num={chunk}", "--force", DEV]
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
            if r.returncode != 0:
                errors += 1
                break
            trimmed += 1
            lba_start += chunk
            lba_count -= chunk
        sectors_done += range_sectors

        now = time.time()
        if now - last_log >= 60:
            elapsed = now - t0
            pct = (sectors_done / total_sectors * 100) if total_sectors else 0
            rate = sectors_done / elapsed if elapsed > 0 else 0
            remaining_sectors = total_sectors - sectors_done
            eta_s = remaining_sectors / rate if rate > 0 else 0
            eta_m = eta_s / 60
            msg = (f"TRIM progress: {i+1}/{len(ranges)} ranges, "
                   f"{pct:.1f}%, {errors} errors, "
                   f"ETA {eta_m:.1f} min")
            print(msg)
            syslog.syslog(msg)
            last_log = now

    return trimmed, errors


MOUNT_POINT = "/var/media/CACHE_DRIVE"


def freeze_disk():
    """Remount CACHE_DRIVE read-only to prevent ALL writes during TRIM."""
    subprocess.run(["sync"], check=False)
    time.sleep(1)
    r = subprocess.run(["mount", "-o", "remount,ro", MOUNT_POINT],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print(f"WARNING: remount ro failed: {r.stderr.strip()}")
        print("Falling back to SIGSTOP on warmup-rs processes")
        return _freeze_processes()
    return "ro"


def thaw_disk(state):
    """Remount CACHE_DRIVE read-write and resume any frozen processes."""
    if state == "ro":
        subprocess.run(["mount", "-o", "remount,rw", MOUNT_POINT], check=False)
    else:
        _thaw_processes(state)


def _freeze_processes():
    """Fallback: SIGSTOP all warmup-rs processes."""
    import signal
    pids = []
    try:
        out = subprocess.check_output(["pidof", "warmup-rs"], text=True).strip()
        pids = [int(p) for p in out.split()]
    except (subprocess.CalledProcessError, ValueError):
        pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGSTOP)
        except OSError:
            pass
    if pids:
        subprocess.run(["sync"], check=False)
        time.sleep(1)
    return pids


def _thaw_processes(pids):
    """Fallback: SIGCONT previously frozen processes."""
    import signal
    for pid in pids:
        try:
            os.kill(pid, signal.SIGCONT)
        except OSError:
            pass


def main():
    syslog.openlog("ssd-trim", syslog.LOG_PID, syslog.LOG_USER)

    state = freeze_disk()
    if state == "ro":
        print(f"remounted {MOUNT_POINT} read-only — no writes possible during TRIM")
        syslog.syslog(f"remounted {MOUNT_POINT} read-only for TRIM")
    else:
        print(f"frozen {len(state)} warmup-rs processes (remount failed, fallback)")
        syslog.syslog(f"frozen {len(state)} PIDs (remount-ro fallback)")

    try:
        block_size, blocks_per_group, block_count, free_blocks, first_data_block = get_fs_params()
        num_groups = (block_count + blocks_per_group - 1) // blocks_per_group

        free_gb = (free_blocks * block_size) / (1024 ** 3)
        print(f"ext4: {block_count} blocks, {free_blocks} free ({free_gb:.1f} GB), {num_groups} groups")
        syslog.syslog(f"starting TRIM: {free_blocks} free blocks ({free_gb:.1f} GB) across {num_groups} groups")

        with open(PART, "rb") as fd:
            bitmaps = read_group_descriptors(fd, block_size, num_groups, first_data_block)
            print(f"read {len(bitmaps)} group descriptors")

            lba_ranges = []
            total_free = 0
            t0 = time.time()

            for group_idx, bitmap_block in enumerate(bitmaps):
                remaining = block_count - (group_idx * blocks_per_group)
                blocks_in_group = min(blocks_per_group, remaining)

                for rel_start, count in read_block_bitmap(fd, bitmap_block, block_size, blocks_in_group):
                    abs_block = group_idx * blocks_per_group + first_data_block + rel_start
                    lba_start = abs_block * SECTORS_PER_BLOCK + PART_START_SECTORS
                    lba_count = count * SECTORS_PER_BLOCK
                    lba_ranges.append((lba_start, lba_count))
                    total_free += count

                if (group_idx + 1) % 500 == 0:
                    print(f"  scanned {group_idx + 1}/{num_groups} groups, {len(lba_ranges)} free ranges so far")

        scan_time = time.time() - t0
        free_found_gb = (total_free * block_size) / (1024 ** 3)
        print(f"scan complete: {len(lba_ranges)} free ranges ({free_found_gb:.1f} GB) in {scan_time:.1f}s")
        syslog.syslog(f"scan: {len(lba_ranges)} ranges ({free_found_gb:.1f} GB) in {scan_time:.1f}s")

        merged = coalesce_ranges(lba_ranges)
        print(f"coalesced to {len(merged)} ranges (from {len(lba_ranges)})")

        total_sectors = sum(c for _, c in merged)
        print(f"sending TRIM via sg_unmap ({len(merged)} ranges, {total_sectors} sectors, max {MAX_UNMAP_LBA} LBAs each)...")
        syslog.syslog(f"TRIM starting: {len(merged)} ranges, {total_sectors * 512 / (1024**3):.1f} GB")
        t1 = time.time()
        trimmed, errors = trim_ranges(merged, total_sectors)
        trim_time = time.time() - t1

        print(f"TRIM complete: {trimmed} commands in {trim_time:.1f}s, {errors} errors")
        syslog.syslog(f"TRIM complete: {trimmed} commands in {trim_time:.1f}s, {errors} errors")
    finally:
        thaw_disk(state)
        if state == "ro":
            print(f"remounted {MOUNT_POINT} read-write")
            syslog.syslog(f"remounted {MOUNT_POINT} read-write after TRIM")
        else:
            print(f"resumed {len(state)} warmup-rs processes")
            syslog.syslog(f"resumed {len(state)} PIDs after TRIM")


if __name__ == "__main__":
    main()
