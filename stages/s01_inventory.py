"""Stage 01 — inventory of the raw flight-line LAZ: header, CRS, extent,
point count and footprint density per tile. Writes a table (CSV + JSON).
"""
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def outputs(cfg):
    return [common.out(cfg, "01_inventory", "inventory.csv"),
            common.out(cfg, "01_inventory", "inventory.json")]


def _header(path):
    md = json.loads(subprocess.run(["pdal", "info", path, "--metadata"],
                                   capture_output=True, text=True).stdout)["metadata"]
    dx = md["maxx"] - md["minx"]
    dy = md["maxy"] - md["miny"]
    area = dx * dy
    srs = md.get("srs", {}).get("json", {})
    epsg = srs.get("id", {}).get("code") if isinstance(srs, dict) else None
    return {
        "tile": os.path.basename(path),
        "points": md["count"],
        "epsg": epsg,
        "minx": md["minx"], "maxx": md["maxx"],
        "miny": md["miny"], "maxy": md["maxy"],
        "minz": md["minz"], "maxz": md["maxz"],
        "footprint_m2": round(area, 1),
        "density_pts_m2": round(md["count"] / area, 2) if area else None,
        "point_format": md.get("dataformat_id"),
        "software": md.get("software_id"),
    }


def run(cfg, force=False):
    outs = outputs(cfg)
    if common.should_skip(cfg, "s01_inventory", outs, force):
        common.note_skip(cfg, "s01_inventory")
        return
    t0 = time.time()
    files = sorted(glob.glob(os.path.join(cfg["paths"]["_input_laz_dir"], "*.laz")))
    if not files:
        raise common.QCFailure("no LAZ found in %s" % cfg["paths"]["_input_laz_dir"])
    rows = [_header(f) for f in files]

    csv_path, json_path = outs
    cols = list(rows[0].keys())
    with open(csv_path, "w") as fh:
        fh.write(",".join(cols) + "\n")
        for r in rows:
            fh.write(",".join(str(r[c]) for c in cols) + "\n")
    total = sum(r["points"] for r in rows)
    summary = {"n_tiles": len(rows), "total_points": total, "tiles": rows}
    with open(json_path, "w") as fh:
        json.dump(summary, fh, indent=1)

    print("%-52s %12s  %7s  %s" % ("tile", "points", "d/m2", "epsg"))
    for r in rows:
        print("%-52s %12d  %7.2f  %s" %
              (r["tile"], r["points"], r["density_pts_m2"], r["epsg"]))
    print("TOTAL %46s %12d" % ("", total))
    common.record_stage(cfg, "s01_inventory", time.time() - t0,
                        {"n_tiles": len(rows), "total_points": total}, outs)


if __name__ == "__main__":
    common.standalone(run)
