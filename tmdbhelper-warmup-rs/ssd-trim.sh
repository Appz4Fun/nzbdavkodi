#!/bin/sh
# TRIM free space on CACHE_DRIVE via hdparm ATA passthrough.
# Works around kernel 4.9 not exposing TRIM for USB-attached SSDs.
# The Samsung T5's USB bridge supports ATA SAT passthrough for TRIM
# but the kernel's USB storage driver doesn't advertise it.

DEV=/dev/sda
PART_START=$(cat /sys/block/sda/sda1/start)
SECTORS_PER_BLOCK=8  # 4096-byte fs blocks / 512-byte sectors

logger -t ssd-trim "starting TRIM on $DEV (partition offset=$PART_START)"

TMPFILE=$(mktemp)
RANGES=0

# Parse free block ranges from ext4 block group descriptors.
dumpe2fs /dev/sda1 2>/dev/null | grep "Free blocks:" | sed 's/.*Free blocks: //' | tr ',' '\n' | while read range; do
    case "$range" in
        *-*)
            START=${range%%-*}
            END=${range##*-}
            ;;
        ""|" ")
            continue
            ;;
        *)
            START=$range
            END=$range
            ;;
    esac
    LBA_START=$(( START * SECTORS_PER_BLOCK + PART_START ))
    LBA_COUNT=$(( (END - START + 1) * SECTORS_PER_BLOCK ))
    echo "${LBA_START}:${LBA_COUNT}"
done > "$TMPFILE"

TOTAL=$(wc -l < "$TMPFILE")
logger -t ssd-trim "found $TOTAL free ranges to TRIM"

# hdparm TRIM limit: 8 LBA ranges per ATA DATA SET MANAGEMENT command.
BATCH=""
COUNT=0
TRIMMED=0
while read range; do
    if [ -z "$BATCH" ]; then
        BATCH="$range"
    else
        BATCH="$BATCH $range"
    fi
    COUNT=$((COUNT + 1))
    if [ "$COUNT" -ge 8 ]; then
        hdparm --trim-sector-ranges $BATCH --please-destroy-my-drive "$DEV" >/dev/null 2>&1
        TRIMMED=$((TRIMMED + COUNT))
        BATCH=""
        COUNT=0
        # Progress every 1000 batches
        if [ $((TRIMMED % 8000)) -eq 0 ]; then
            logger -t ssd-trim "progress: $TRIMMED / $TOTAL ranges trimmed"
        fi
    fi
done < "$TMPFILE"

# Flush remaining
if [ -n "$BATCH" ]; then
    hdparm --trim-sector-ranges $BATCH --please-destroy-my-drive "$DEV" >/dev/null 2>&1
    TRIMMED=$((TRIMMED + COUNT))
fi

rm -f "$TMPFILE"
logger -t ssd-trim "TRIM complete: $TRIMMED ranges trimmed"
echo "TRIM complete: $TRIMMED / $TOTAL ranges trimmed"
