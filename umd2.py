#!/usr/bin/env python3
"""
Enhanced UMD2 backend (no GUI): serial/file -> tokens -> computations -> JSONL/CSV

New features beyond the basic D/N pipeline:
  - Modes: displacement (default) or angle
  - Lambda/scaling variants: --lambda-nm and --scale-div (2|4|8) to define per-count nanometers
  - Smoothing: --ema-alpha (0..1), --ma-window (samples)
  - Environmental compensation: linear scale with temperature/pressure/humidity deltas
  - FFT snapshots: --fft-len, --fft-every, --fft-signal (x|v)
  - Multi-axis (optional): parse X:/Y: and compute second channel; straightness multiplier
  - File logging: --log path (CSV), or --out csv/jsonl to stdout

Input tokens recognized (case-insensitive, any order):
  - D:<int> N:<int>        (primary channel counts and sequence)
  - X:<float> Y:<float>    (optional second channel raw values)
  - REF:<int> MEAS:<int> DIFF:<int>  (if firmware emits these, they can be used as D)
  - Header: "Sample Frequency = <num> Hz"

Examples:
  # displacement with lambda/8 scaling @ HeNe 632.991 nm
  python umd2.py --file data.txt --lambda-nm 632.991 --scale-div 8 --emit onstep

  # angle mode using normalized nm scale for asin(), with correction factor
  python umd2.py --file data.txt --mode angle --angle-norm-nm 5000 --angle-corr 1.0

  # real-time serial with smoothing and FFT of velocity
  python umd2.py --serial /dev/tty.usbmodem1101 --baud 921600 --ema-alpha 0.2 --fft-len 2048 --fft-every 200 --fft-signal v

CSV/JSON schema (per "data" line):
  seq, fs_hz, D, deltaD, step_nm, x_nm, v_nm_s, x_nm_ema, x_nm_ma, x_nm_env, angle_deg, x2, y2
FFT snapshot lines are emitted as JSON with {"type":"fft","signal":"x|v","fs_hz":...,"freq":[...],"mag":[...]}

"""

import sys
import re
import argparse
import json
import csv
import math
from collections import deque
from typing import Optional, Tuple, Iterator, List

HeaderFS_RE = re.compile(r'^\s*Sample\s+Frequency\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*Hz\s*$', re.IGNORECASE)
Tok_RE = re.compile(r'([A-Za-z]+)\s*:\s*(-?\d+(?:\.\d+)?)\b')

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="UMD2 parser & calculator (enhanced)")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--serial", help="Serial device path (e.g., /dev/tty.usbmodem1101)")
    p.add_argument("--baud", type=int, default=115200, help="Baud rate for --serial (default: 115200)")
    src.add_argument("--file", help="Input file path; if omitted and --serial not set, read stdin")

    # Core
    p.add_argument("--fs", type=float, default=0.0, help="Override sample frequency in Hz (default: 0=auto/header or 1000)")
    p.add_argument("--emit", choices=["every","onstep"], default="every", help="Emit every sample or only on deltaD != 0 (default: every)")
    p.add_argument("--decimate", type=int, default=1, help="After emit filter, output only every Nth kept record (default: 1)")

    # Displacement scaling
    p.add_argument("--stepnm", type=float, default=None, help="Explicit nm per deltaD (overrides lambda/scale-div if set)")
    p.add_argument("--lambda-nm", type=float, default=632.991, help="Laser wavelength in nm (default HeNe ~632.991)")
    p.add_argument("--scale-div", type=int, default=8, choices=[1,2,4,8], help="Interferometer division (1,2,4,8); default 8")
    p.add_argument("--startnm", type=float, default=0.0, help="Starting baseline for x_nm (default: 0)")
    p.add_argument("--straight-mult", type=float, default=1.0, help="Multiply x_nm by this (straightness correction).")

    # Mode: displacement or angle
    p.add_argument("--mode", choices=["displacement","angle"], default="displacement", help="Computation mode for secondary output")
    p.add_argument("--angle-norm-nm", type=float, default=1.0, help="Normalization nm for asin argument (dx_norm = x_nm / angle-norm-nm)")
    p.add_argument("--angle-corr", type=float, default=1.0, help="Angle correction factor, applied before deg conversion")
    # angle = asin( clamp( x_nm / angle_norm_nm, -1..1 ) ) * angle_corr * 57.296

    # Smoothing
    p.add_argument("--ema-alpha", type=float, default=0.0, help="EMA alpha (0 disables EMA)")
    p.add_argument("--ma-window", type=int, default=0, help="Moving average window (0 disables MA)")

    # Environmental compensation (linear scale)
    p.add_argument("--env-temp", type=float, default=None, help="Current temperature (C)")
    p.add_argument("--env-temp0", type=float, default=None, help="Reference temperature (C)")
    p.add_argument("--env-ktemp", type=float, default=0.0, help="Scale per degC; x_env = x * (1 + ktemp*(T-T0))")
    p.add_argument("--env-press", type=float, default=None, help="Current pressure")
    p.add_argument("--env-press0", type=float, default=None, help="Reference pressure")
    p.add_argument("--env-kpress", type=float, default=0.0, help="Scale per pressure unit; x_env = x * (1 + kpress*(P-P0))")
    p.add_argument("--env-hum", type=float, default=None, help="Current relative humidity (%)")
    p.add_argument("--env-hum0", type=float, default=None, help="Reference RH (%)")
    p.add_argument("--env-khum", type=float, default=0.0, help="Scale per RH; x_env = x * (1 + khum*(H-H0))")

    # FFT
    p.add_argument("--fft-len", type=int, default=0, help="FFT window length (0 disables). Use power of two like 1024/2048.")
    p.add_argument("--fft-every", type=int, default=0, help="Emit FFT every N emitted samples (0 disables).")
    p.add_argument("--fft-signal", choices=["x","v"], default="x", help="Signal to FFT: displacement (x) or velocity (v).")

    # Secondary axis input (optional)
    p.add_argument("--enable-xy", action="store_true", help="Parse X:/Y: tokens as a second channel (x2,y2)")

    # Output
    p.add_argument("--out", choices=["jsonl","csv"], default="jsonl", help="Output format to stdout (default: jsonl)")
    p.add_argument("--log", type=str, default=None, help="If set, append a CSV log to this path.")

    return p.parse_args(argv)

def iter_lines_serial(port: str, baud: int):
    try:
        import serial  # pyserial
    except Exception:
        print("ERROR: pyserial is required for --serial. Install with: pip install pyserial", file=sys.stderr)
        sys.exit(2)
    ser = serial.Serial(port, baudrate=baud, timeout=0.2)
    try:
        buf = b""
        while True:
            chunk = ser.read(4096)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.decode(errors="ignore")
            else:
                pass
    finally:
        try: ser.close()
        except: pass

def iter_lines_file(path: str):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            yield line

def iter_lines_stdin():
    for line in sys.stdin:
        yield line

def parse_line_tokens(line: str):
    """
    Returns dict of tokens found, e.g. {"D":1234, "N":56, "X":0.12, "Y":0.34, "REF":..., "MEAS":..., "DIFF":...}
    Recognizes FS header separately.
    """
    out = {}
    for key, val in Tok_RE.findall(line):
        k = key.upper()
        try:
            v = float(val) if '.' in val else int(val)
        except ValueError:
            continue
        out[k] = v
    return out

def maybe_extract_fs(line: str) -> Optional[float]:
    m = HeaderFS_RE.match(line)
    if m:
        try:
            return float(m.group(1))
        except:
            return None
    return None

def clamp(val, lo, hi):
    return lo if val < lo else hi if val > hi else val

def compute_step_nm(args) -> float:
    if args.stepnm is not None:
        return float(args.stepnm)
    # derive from wavelength / division (per-count nm)
    div = max(1, int(args.scale_div))
    return float(args.lambda_nm) / div

def apply_env(x_nm: float, args) -> float:
    scale = 1.0
    if args.env_temp is not None and args.env_temp0 is not None and args.env_ktemp != 0.0:
        scale *= (1.0 + args.env_ktemp * (args.env_temp - args.env_temp0))
    if args.env_press is not None and args.env_press0 is not None and args.env_kpress != 0.0:
        scale *= (1.0 + args.env_kpress * (args.env_press - args.env_press0))
    if args.env_hum is not None and args.env_hum0 is not None and args.env_khum != 0.0:
        scale *= (1.0 + args.env_khum * (args.env_hum - args.env_hum0))
    return x_nm * scale

def angle_from_displacement(x_nm: float, args) -> float:
    # angle = asin( clamp( x_nm / angle_norm_nm, -1..1 ) ) * angle_corr * 57.296
    if args.angle_norm_nm == 0:
        return 0.0
    norm = clamp(x_nm / args.angle_norm_nm, -1.0, 1.0)
    return math.asin(norm) * args.angle_corr * 57.296

def main(argv=None):
    args = parse_args(argv)

    # Source
    if args.serial:
        source = iter_lines_serial(args.serial, args.baud)
    elif args.file:
        source = iter_lines_file(args.file)
    else:
        source = iter_lines_stdin()

    fs_hz = args.fs if args.fs > 0 else 0.0
    step_nm_per_count = compute_step_nm(args)
    x_nm = float(args.startnm)
    prevD = None

    # Smoothing buffers
    ema_x = None
    ma_buf: deque = deque(maxlen=args.ma_window if args.ma_window > 0 else 1)

    # FFT buffers
    fft_buf: List[float] = []
    emitted = 0

    # Logging
    log_file = None
    log_writer = None
    if args.log:
        log_exists = os.path.exists(args.log)
        log_file = open(args.log, "a", newline="")
        log_writer = csv.writer(log_file)
        if not log_exists:
            log_writer.writerow(["seq","fs_hz","D","deltaD","step_nm","x_nm","v_nm_s","x_nm_ema","x_nm_ma","x_nm_env","angle_deg","x2","y2"])

    # Output CSV to stdout
    stdout_writer = None
    if args.out == "csv":
        stdout_writer = csv.writer(sys.stdout, lineterminator="\n")
        stdout_writer.writerow(["seq","fs_hz","D","deltaD","step_nm","x_nm","v_nm_s","x_nm_ema","x_nm_ma","x_nm_env","angle_deg","x2","y2"])

    for raw in source:
        line = raw.strip()
        if not line:
            continue

        fs_found = maybe_extract_fs(line)
        if fs_found:
            fs_hz = fs_found
            continue

        toks = parse_line_tokens(line)
        # Map REF/MEAS/DIFF if provided; DIFF takes precedence, else D
        D = None
        if "DIFF" in toks:
            D = int(toks["DIFF"])
        elif "D" in toks:
            D = int(toks["D"])
        else:
            # no usable primary count -> skip
            continue

        N = int(toks.get("N", 0))

        # Optional second channel
        x2 = float(toks["X"]) if ("X" in toks and args.enable_xy) else None
        y2 = float(toks["Y"]) if ("Y" in toks and args.enable_xy) else None

        if fs_hz <= 0.0:
            fs_hz = 1000.0

        if prevD is None:
            dD = 0
            prevD = D
        else:
            dD = D - prevD
            prevD = D

        # nm step and cumulative
        dx = step_nm_per_count * float(dD)     # per-sample displacement (nm)
        x_nm = (x_nm + dx) * args.straight_mult

        # velocity
        v_nm_s = dx * fs_hz

        # smoothing
        x_nm_ema = None
        if args.ema_alpha > 0.0:
            if ema_x is None:
                ema_x = x_nm
            else:
                ema_x = args.ema_alpha * x_nm + (1.0 - args.ema_alpha) * ema_x
            x_nm_ema = ema_x

        x_nm_ma = None
        if args.ma_window > 0:
            if ma_buf.maxlen != args.ma_window:
                ma_buf = deque(ma_buf, maxlen=args.ma_window)
            ma_buf.append(x_nm)
            x_nm_ma = sum(ma_buf) / len(ma_buf)

        # environmental compensation applied to *raw cumulative* x_nm
        x_nm_env = apply_env(x_nm, args)

        # angle mode (secondary)
        angle_deg = None
        if args.mode == "angle":
            angle_deg = angle_from_displacement(x_nm, args)

        # emission filtering
        keep = True
        if args.emit == "onstep":
            keep = (dD != 0)

        if keep:
            emitted += 1
            if args.decimate > 1 and (emitted % args.decimate) != 0:
                keep = False

        if not keep:
            continue

        # record
        rec = {
            "seq": int(N),
            "fs_hz": float(fs_hz),
            "D": int(D),
            "deltaD": int(dD),
            "step_nm": float(dx),
            "x_nm": float(x_nm),
            "v_nm_s": float(v_nm_s),
            "x_nm_ema": (float(x_nm_ema) if x_nm_ema is not None else None),
            "x_nm_ma": (float(x_nm_ma) if x_nm_ma is not None else None),
            "x_nm_env": float(x_nm_env),
            "angle_deg": (float(angle_deg) if angle_deg is not None else None),
            "x2": (float(x2) if x2 is not None else None),
            "y2": (float(y2) if y2 is not None else None),
        }

        # stdout
        if args.out == "jsonl":
            sys.stdout.write(json.dumps(rec, separators=(",",":")) + "\n")
        else:
            stdout_writer.writerow([rec["seq"], rec["fs_hz"], rec["D"], rec["deltaD"], rec["step_nm"], rec["x_nm"],
                                    rec["v_nm_s"], rec["x_nm_ema"], rec["x_nm_ma"], rec["x_nm_env"], rec["angle_deg"], rec["x2"], rec["y2"]])

        # log
        if log_writer:
            log_writer.writerow([rec["seq"], rec["fs_hz"], rec["D"], rec["deltaD"], rec["step_nm"], rec["x_nm"],
                                 rec["v_nm_s"], rec["x_nm_ema"], rec["x_nm_ma"], rec["x_nm_env"], rec["angle_deg"], rec["x2"], rec["y2"]])

        # FFT snapshots
        if args.fft_len > 0 and args.fft_every > 0:
            try:
                import numpy as np
            except Exception:
                np = None
            if np is not None and emitted % args.fft_every == 0:
                # accumulate signal
                sigval = x_nm if args.fft_signal == "x" else v_nm_s
                fft_buf.append(sigval)
                if len(fft_buf) >= args.fft_len:
                    buf = np.array(fft_buf[-args.fft_len:], dtype=np.float64)
                    # Hanning window
                    w = np.hanning(len(buf))
                    bufw = buf * w
                    spec = np.fft.rfft(bufw)
                    mag = np.abs(spec)
                    freq = np.fft.rfftfreq(len(bufw), d=(1.0/fs_hz if fs_hz>0 else 0.001))
                    sys.stdout.write(json.dumps({
                        "type":"fft",
                        "signal": args.fft_signal,
                        "fs_hz": float(fs_hz),
                        "freq": freq.tolist(),
                        "mag": mag.tolist()
                    }, separators=(",",":")) + "\n")

    if log_file:
        log_file.flush(); log_file.close()

if __name__ == "__main__":
    main()
