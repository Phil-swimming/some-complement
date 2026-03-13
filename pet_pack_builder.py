#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pet_pack_builder.py

Build V4 PET content pack binary:
  - Act cfg table (timing)
  - Global cfg (env/story)
  - Phrase table (speech bubble words)

Usage:
  python pet_pack_builder.py --out pet_pack_v4.bin
  python pet_pack_builder.py --json pet_pack_v4_default.json --out pet_pack_v4.bin
  python pet_pack_builder.py --json pet_pack_v4_default.json --c-out pet_pack_default_c_array.txt

Notes:
  - Flash address in firmware: 0x00000000 (first 64KB sector)
  - Keep phrases short (<=23 chars), ASCII recommended for 5x7 font
"""
import argparse
import json
import struct
import zlib
from pathlib import Path

PET_PACK_MAGIC = 0x50544550  # "PETP"
PET_PACK_VERSION = 0x00040000

PET_ITEM_ACT_CFG = 1
PET_ITEM_GLOBAL_CFG = 2
PET_ITEM_PHRASES = 3

# MUST match PetAct_t order in App/Src/app_lcd12864.c
ACT_ORDER = [
    "PET_ACT_DAYDREAM",
    "PET_ACT_STROLL",
    "PET_ACT_PLAY",
    "PET_ACT_EAT",
    "PET_ACT_SLEEP",
    "PET_ACT_SNIFF",
    "PET_ACT_STRETCH",
    "PET_ACT_SCRATCH",
    "PET_ACT_SHAKE",
    "PET_ACT_TROT",
    "PET_ACT_LOOK",
    "PET_ACT_LIE",
    "PET_ACT_PEE_TREE",
    "PET_ACT_PLAY_FRIEND",
    "PET_ACT_DIG",
    "PET_ACT_CHASE_BUTTERFLY",
    "PET_ACT_DRINK",
    "PET_ACT_ROLL_GRASS",
]

GLOBAL_FIELDS = [
    "story_phase_retry_ms",
    "story_approach_base_ms",
    "story_approach_per_px_ms",
    "story_approach_reserved",
    "story_approach_max_ms",
    "story_interact_min_ms",
    "story_interact_max_ms",
    "story_chase_min_ms",
    "story_chase_max_ms",
    "story_leave_min_ms",
    "story_leave_max_ms",
    "story_end_next_wp_min_ms",
    "story_end_next_wp_max_ms",
    "story_end_next_act_min_ms",
    "story_end_next_act_max_ms",
    "next_env_min_ms",
    "next_env_max_ms",
    "butterfly_move_min_ms",
    "butterfly_move_max_ms",
]

def align4(x: int) -> int:
    return (x + 3) & ~3

def crc32(b: bytes) -> int:
    return zlib.crc32(b) & 0xFFFFFFFF

def build_phrases(phrases):
    phrases = [p.strip() for p in phrases if p.strip()]
    phrases = [p[:23] for p in phrases]
    blob = b""
    offs = []
    cur = 0
    for p in phrases:
        offs.append(cur)
        bb = p.encode("ascii", errors="ignore") + b"\0"
        blob += bb
        cur += len(bb)
    out = struct.pack("<HH", len(phrases), 0) + b"".join(struct.pack("<I", o) for o in offs) + blob
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=str, default="", help="json config file")
    ap.add_argument("--out", type=str, default="pet_pack_v4.bin", help="output bin")
    ap.add_argument("--c-out", type=str, default="", help="optional: write C array text")
    args = ap.parse_args()

    cfg = {}
    if args.json:
        cfg = json.loads(Path(args.json).read_text(encoding="utf-8"))

    # Acts
    acts = cfg.get("acts", {})
    act_rows = []
    for name in ACT_ORDER:
        a = acts.get(name, {})
        weight = int(a.get("weight", 10))
        frame_ms = int(a.get("frame_ms", 420))
        dmin = int(a.get("dur_min_ms", 9000))
        dmax = int(a.get("dur_max_ms", 20000))
        act_rows.append(struct.pack("<HHII", weight & 0xFFFF, frame_ms & 0xFFFF, dmin & 0xFFFFFFFF, dmax & 0xFFFFFFFF))
    act_bin = b"".join(act_rows)

    # Global
    g = cfg.get("global", {})
    global_vals = []
    for f in GLOBAL_FIELDS:
        global_vals.append(int(g.get(f, 0)))
    # Pack layout must match firmware struct (see pet_pack.h)
    global_bin = struct.pack(
        "<IIHHI"  # retry, base, per_px, reserved, max
        "IIII"    # interact/chase
        "II"      # leave
        "IIII"    # end+env
        "II"      # butterfly
        "II",     # reserved (kept for forward compat)
        global_vals[0], global_vals[1], global_vals[2] & 0xFFFF, global_vals[3] & 0xFFFF, global_vals[4],
        global_vals[5], global_vals[6], global_vals[7], global_vals[8],
        global_vals[9], global_vals[10],
        global_vals[11], global_vals[12], global_vals[13], global_vals[14],
        global_vals[15], global_vals[16],
        global_vals[17], global_vals[18],
        0, 0
    )

    # Phrases
    phrases = cfg.get("phrases", [])
    if not phrases:
        phrases = ["hi", "sniff", "walk", "zzz"]
    phrase_bin = build_phrases(phrases)

    # Build pack
    hdr_size = 32
    idx_count = 3
    idx_size = idx_count * 16
    off = hdr_size + idx_size

    act_off = off
    off = align4(act_off + len(act_bin))
    glob_off = off
    off = align4(glob_off + len(global_bin))
    phr_off = off
    off = align4(phr_off + len(phrase_bin))
    total = off

    idx = [
        (PET_ITEM_ACT_CFG, act_off, len(act_bin), crc32(act_bin)),
        (PET_ITEM_GLOBAL_CFG, glob_off, len(global_bin), crc32(global_bin)),
        (PET_ITEM_PHRASES, phr_off, len(phrase_bin), crc32(phrase_bin)),
    ]
    idx_bin = b"".join(struct.pack("<IIII", *e) for e in idx)

    build_id = int(cfg.get("build_id", 20260305))
    hdr0 = struct.pack("<IIIIIIII", PET_PACK_MAGIC, PET_PACK_VERSION, total, hdr_size, idx_count, build_id, 0, 0)
    data = bytearray(total - (hdr_size + idx_size))
    data[act_off - (hdr_size + idx_size): act_off - (hdr_size + idx_size) + len(act_bin)] = act_bin
    data[glob_off - (hdr_size + idx_size): glob_off - (hdr_size + idx_size) + len(global_bin)] = global_bin
    data[phr_off - (hdr_size + idx_size): phr_off - (hdr_size + idx_size) + len(phrase_bin)] = phrase_bin

    crc = crc32(hdr0 + idx_bin + data)
    hdr = struct.pack("<IIIIIIII", PET_PACK_MAGIC, PET_PACK_VERSION, total, hdr_size, idx_count, build_id, crc, 0)
    pack = hdr + idx_bin + data

    Path(args.out).write_bytes(pack)
    print("Wrote:", args.out, "size=", len(pack))

    if args.c_out:
        bb = ", ".join(f"0x{x:02X}" for x in pack)
        Path(args.c_out).write_text(bb, encoding="utf-8")
        print("Wrote:", args.c_out)

if __name__ == "__main__":
    main()
