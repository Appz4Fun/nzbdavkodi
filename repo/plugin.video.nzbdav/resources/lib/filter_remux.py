# SPDX-License-Identifier: GPL-3.0-or-later
"""REMUX relevance priorities and TRaSH group definitions retrieved 2026-09-05."""

import re

REMUX_TIERS = (
    # https://raw.githubusercontent.com/TRaSH-Guides/Guides/master/docs/json/radarr/cf/remux-tier-01.json
    (
        ("3L", r"^(3L)$"),
        ("ATELiER", r"^(ATELiER)$"),
        ("BiZKiT", r"^(BiZKiT)$"),
        ("BLURANiUM", r"^(BLURANiUM)$"),
        ("BMF", r"^(BMF)$"),
        ("CiNEPHiLES", r"^(CiNEPHiLES)$"),
        ("FraMeSToR", r"^(FraMeSToR)$"),
        ("PiRAMiDHEAD", r"^(PiRAMiDHEAD)$"),
        ("PmP", r"^(PmP)$"),
        ("WiLDCAT", r"^(WiLDCAT)$"),
        ("ZQ", r"^(ZQ)$"),
    ),
    # https://raw.githubusercontent.com/TRaSH-Guides/Guides/master/docs/json/radarr/cf/remux-tier-02.json
    (
        ("NCmt", r"^(NCmt)$"),
        ("playBD", r"^(playBD)$"),
        ("SiCFoI", r"^(SiCFoI)$"),
        ("SURFINBIRD", r"^(SURFINBIRD)$"),
        ("TEPES", r"^(TEPES)$"),
    ),
    # https://raw.githubusercontent.com/TRaSH-Guides/Guides/master/docs/json/radarr/cf/remux-tier-03.json
    (
        ("12GaugeShotgun", r"^(12GaugeShotgun)$"),
        ("decibeL", r"^(decibeL)$"),
        ("EPSiLON", r"^(EPSiLON)$"),
        ("HiFi", r"^(HiFi)$"),
        ("iFT", r"^(iFT)$"),
        ("KRaLiMaRKo", r"^(KRaLiMaRKo)$"),
        ("NTb", r"^(NTb)$"),
        ("PTP", r"^(PTP)$"),
        ("SumVision", r"^(SumVision)$"),
        ("TOA", r"^(TOA)$"),
        ("TRiToN", r"^(TRiToN)$"),
    ),
)

DEFAULT_REMUX_GROUPS = tuple(tuple(name for name, _ in tier) for tier in REMUX_TIERS)
_REMUX = re.compile(r"(?<![a-z0-9])remux(?![a-z0-9])", re.I)
_HYBRID = re.compile(r"(?<![a-z0-9])hybrid(?![a-z0-9])", re.I)
_PUBLISHED_PATTERNS = {
    name.lower(): pattern for tier in REMUX_TIERS for name, pattern in tier
}


def read_remux_tiers(settings_getter):
    """Use bundled defaults for absent settings, preserving explicit empty tiers."""
    tiers = []
    for number, groups in enumerate(DEFAULT_REMUX_GROUPS, 1):
        raw = settings_getter("filter_remux_tier_{}".format(number), ",".join(groups))
        tiers.append([name.strip() for name in raw.split(",") if name.strip()])
    return tiers


def compile_remux_tiers(tiers):
    """Compile published group patterns; custom names are exact literal matches."""
    return [
        [
            re.compile(_PUBLISHED_PATTERNS.get(group.lower(), re.escape(group)), re.I)
            for group in tier
        ]
        for tier in tiers
    ]


def remux_priority(result):
    """Hybrid REMUX first, REMUX second, then everything else."""
    title = result.get("title", "")
    if not _REMUX.search(title):
        return 2
    return 0 if _HYBRID.search(title) else 1


def remux_group_rank(result, tiers):
    """Return the first matching preferred tier for a parsed release group."""
    group = result.get("_meta", {}).get("group", "")
    return next(
        (
            rank
            for rank, patterns in enumerate(tiers)
            if any(pattern.fullmatch(group) for pattern in patterns)
        ),
        len(tiers),
    )
