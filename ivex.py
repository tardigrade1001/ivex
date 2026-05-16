"""
ivex.py - Convert Keysight B2900-series Source/Measure Unit .QIVD binary files to plain CSV.

The B2900-series SMU "B291x Utility Software" (a.k.a. Quick I-V) saves measurements as
a proprietary .QIVD binary by default, with an optional checkbox to also emit a CSV at
save time. If that checkbox is missed the data is locked to the instrument PC.

This script reads the .QIVD format directly and writes a clean CSV next to each file.

Usage:
    python ivex.py                  # convert every .qivd under the current folder
    python ivex.py "C:/path/to/dir" # convert every .qivd under that folder

Output:
    For each foo.qivd, writes foo.csv and foo.png next to it. An _overlay.png that
    plots every file together is written to plots/ in the scan root. A summary
    log (ivex.log) is written to the scan root.

Format notes (reverse-engineered):

    Magic bytes:  "'B291x Utility Software Save File Header"
    Body:         a stream of TLV (tag-length-value) records, all little-endian.
                  Each record:  uint32 tag | uint32 length | length bytes payload.
                  tag = 1  -> ASCII column name (e.g. "CH1 Voltage 1")
                  tag = 4,5,6 -> 4-byte uint32 metadata (data-type / flags)
                  tag = 7  -> the column's float64 data array
    Layout:       columns are stored back-to-back. The standard 6 are
                  CH1 Voltage, CH1 Current, CH1 Resistance, CH1 Power, CH1 Time, CH1 State.
                  Some files also carry "CH1 Source" and per-column duplicates
                  appended for the saved-view block; we keep the first occurrence.

Verified against 17 ground-truth Keysight CSV exports (worst relative error ~5e-6,
which is Keysight's 5-significant-figure display precision).
"""

import os
import re
import struct
import sys
import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PLOT_COLOR = "#e91e63"  # pink, matching spectrex

MAGIC = b"'B291x Utility Software Save File Header"

# Columns to emit (in this order) when present in the file.
STANDARD_COLUMNS = ["Voltage", "Current", "Resistance", "Power", "Time", "State"]


class QivdParseError(Exception):
    pass


def parse_qivd(path):
    """Parse a .qivd file.

    Returns (meta, columns) where:
      meta is a dict with keys: visa_address, instrument_strings, raw_size
      columns is an ordered dict { "CH1 Voltage": [floats], ... }
    """
    data = Path(path).read_bytes()
    if not data.startswith(MAGIC):
        raise QivdParseError(f"unrecognized magic bytes (not a B2900 .qivd file)")

    meta = {"raw_size": len(data)}

    # VISA address is the first ASCII run starting with "USB" or "TCPIP" or "GPIB".
    m = re.search(rb'(USB|TCPIP|GPIB)[0-9A-Za-z:_\.]+::INSTR', data[:512])
    meta["visa_address"] = m.group(0).decode("ascii") if m else ""

    # Pull a few descriptive ASCII tokens from the header region for the CSV comment block.
    tokens = []
    for tm in re.finditer(rb'[ -~]{4,}', data[:768]):
        s = tm.group().decode("ascii", "replace")
        if s.startswith("'B291x"):
            continue
        if meta["visa_address"] and s in meta["visa_address"]:
            continue
        tokens.append(s)
    meta["header_tokens"] = tokens

    # Walk the file looking for column-name records. A column header has the form
    #   <uint32 name_len> <name_bytes> 04 00 00 00 04 00 00 00   <-- tag=4 len=4 starts here
    # so we anchor on the "tag=4 len=4" sentinel and confirm the preceding name length.
    cols = {}
    name_pat = re.compile(rb'(CH\d [A-Za-z]+(?: [A-Za-z]+)?)((?: \d+)?)\x04\x00\x00\x00\x04\x00\x00\x00')
    for m in name_pat.finditer(data):
        base = m.group(1).decode("ascii")
        suffix = m.group(2).decode("ascii")  # may be "", " 1", " 2", ...
        full_name = base + suffix
        name_len_off = m.start() - 4
        if name_len_off < 0:
            continue
        name_len = struct.unpack_from('<I', data, name_len_off)[0]
        if name_len != len(full_name):
            continue
        # Scan forward up to ~80 bytes for the tag-7 data marker.
        search_from = m.end()
        idx = data.find(b'\x07\x00\x00\x00', search_from, search_from + 80)
        if idx < 0:
            continue
        size = struct.unpack_from('<I', data, idx + 4)[0]
        data_off = idx + 8
        if size <= 0 or size % 8 != 0 or data_off + size > len(data):
            continue
        n = size // 8
        # Some files duplicate the column block in a saved-view region; keep the first.
        if base in cols:
            continue
        cols[base] = list(struct.unpack_from(f'<{n}d', data, data_off))

    if not cols:
        raise QivdParseError("no column data blocks found")
    return meta, cols


def write_csv(qivd_path, meta, cols, out_path):
    """Write a clean CSV with a metadata comment header."""
    # Determine columns to emit (only those that exist in this file).
    emit = [("CH1 " + name) for name in STANDARD_COLUMNS if ("CH1 " + name) in cols]
    if not emit:
        # Fall back to whatever we have, in their original order.
        emit = list(cols.keys())

    arrays = [cols[k] for k in emit]
    n_points = min(len(a) for a in arrays)
    n_cols = len(emit)

    # Pretty units for the comment header.
    units = {
        "CH1 Voltage":     "V",
        "CH1 Current":     "A",
        "CH1 Resistance":  "ohm",
        "CH1 Power":       "W",
        "CH1 Time":        "s",
        "CH1 State":       "",
    }
    t_arr = cols.get("CH1 Time")
    duration = (t_arr[n_points - 1] - t_arr[0]) if (t_arr and n_points >= 2) else None
    step = ((t_arr[1] - t_arr[0]) if (t_arr and n_points >= 2) else None)

    lines = []
    lines.append(f"# Source: Keysight B291x SMU (B291x Utility Software .qivd)")
    lines.append(f"# Sample: {Path(qivd_path).stem}")
    if meta.get("visa_address"):
        lines.append(f"# Instrument VISA: {meta['visa_address']}")
    lines.append(f"# Points: {n_points}")
    if step is not None:
        lines.append(f"# Sample step: {step:.6g} s    Duration: {duration:.6g} s")
    lines.append(f"# Columns: {', '.join(emit)}")
    # Friendly column header row uses snake_case + unit.
    pretty = []
    for k in emit:
        u = units.get(k, "")
        base = k.replace("CH1 ", "").lower()
        pretty.append(f"{base}_{u}" if u else base)
    lines.append(",".join(pretty))

    # Data rows.
    for i in range(n_points):
        row = []
        for a in arrays:
            v = a[i]
            # Match-engineering-but-clean: keep full float64 precision via repr() w/o trailing junk.
            if v == 0.0:
                row.append("0")
            elif abs(v) < 1e-3 or abs(v) >= 1e6:
                row.append(f"{v:.10e}")
            else:
                row.append(f"{v:.10g}")
        lines.append(",".join(row))

    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n_points, n_cols


def _series_for_plot(cols):
    """Pick whichever of Voltage / Current is actually varying (the measured channel).
    The other one is being sourced and will be ~constant."""
    t = cols.get("CH1 Time")
    if not t:
        return None, None, None
    i = cols.get("CH1 Current")
    v = cols.get("CH1 Voltage")

    def cv(a):
        if not a: return -1.0
        mean = sum(a) / len(a)
        if abs(mean) < 1e-15:
            # zero-centred: fall back to peak-to-peak / 1 to break ties
            return max(a) - min(a)
        var = sum((x - mean) ** 2 for x in a) / len(a)
        return (var ** 0.5) / abs(mean)

    cv_i, cv_v = cv(i), cv(v)
    # Whichever channel has higher coefficient of variation is the measurement.
    if cv_v >= cv_i and v:
        return t, v, "Voltage (V)"
    if i:
        return t, i, "Current (A)"
    if v:
        return t, v, "Voltage (V)"
    return None, None, None


def write_plot(qivd_path, cols, png_path):
    """Single-file plot: pink current-vs-time line, like spectrex."""
    t, y, ylabel = _series_for_plot(cols)
    if t is None:
        return False
    n = min(len(t), len(y))
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(t[:n], y[:n], lw=1.5, color=PLOT_COLOR)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.set_title(Path(qivd_path).stem)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(png_path, dpi=130)
    plt.close(fig)
    return True


def write_overlay(items, png_path):
    """Overlay every successfully-parsed file on one measurement-vs-time plot."""
    fig, ax = plt.subplots(figsize=(10, 6))
    plotted = 0
    ylabels = set()
    for name, cols in items:
        t, y, ylabel = _series_for_plot(cols)
        if t is None:
            continue
        n = min(len(t), len(y))
        ax.plot(t[:n], y[:n], lw=1.2, label=name)
        ylabels.add(ylabel)
        plotted += 1
    if plotted == 0:
        plt.close(fig)
        return False
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(" / ".join(sorted(ylabels)) if len(ylabels) > 1 else next(iter(ylabels)))
    ax.set_title("Source-meter sweeps (overlay)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=7, loc="best")
    fig.tight_layout()
    fig.savefig(png_path, dpi=130)
    plt.close(fig)
    return True


def convert_one(qivd_path, log):
    try:
        meta, cols = parse_qivd(qivd_path)
    except QivdParseError as e:
        log.error(f"FAIL [parse] {qivd_path}: {e}")
        return None
    except Exception as e:
        log.exception(f"FAIL [parse] {qivd_path}: {e}")
        return None

    csv_path = Path(qivd_path).with_suffix(".csv")
    try:
        n_points, n_cols = write_csv(qivd_path, meta, cols, csv_path)
    except Exception as e:
        log.exception(f"FAIL [csv] {qivd_path}: {e}")
        return None

    png_path = Path(qivd_path).with_suffix(".png")
    try:
        write_plot(qivd_path, cols, png_path)
    except Exception as e:
        log.exception(f"FAIL [png] {qivd_path}: {e}")
        # CSV already written, count as success

    log.info(f"OK   {Path(qivd_path).name}: {n_points} pts x {n_cols} cols -> {csv_path.name}")
    return cols


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    root = root.resolve()
    if not root.exists():
        print(f"path not found: {root}", file=sys.stderr)
        sys.exit(2)

    log_path = (root if root.is_dir() else root.parent) / "ivex.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[logging.FileHandler(log_path, mode="w", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("ivex")

    if root.is_file():
        targets = [root]
    else:
        targets = sorted(p for p in root.rglob("*") if p.suffix.lower() == ".qivd")

    if not targets:
        log.info(f"no .qivd files found under {root}")
        return

    ok = fail = 0
    overlay_items = []
    for p in targets:
        cols = convert_one(p, log)
        if cols is None:
            fail += 1
        else:
            ok += 1
            overlay_items.append((p.stem, cols))

    if overlay_items:
        plots_dir = (root if root.is_dir() else root.parent) / "plots"
        plots_dir.mkdir(exist_ok=True)
        overlay_path = plots_dir / "_overlay.png"
        try:
            if write_overlay(overlay_items, overlay_path):
                log.info(f"  -> {overlay_path.relative_to(plots_dir.parent)}")
        except Exception as e:
            log.exception(f"FAIL [overlay]: {e}")

    log.info(f"\nDone. {ok} succeeded, {fail} failed. Log: {log_path}")


if __name__ == "__main__":
    main()
