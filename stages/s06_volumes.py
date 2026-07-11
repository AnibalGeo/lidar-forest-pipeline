"""Stage 06 — stockpile volumes via stockpile_volumes.py. Dual method:
(a) point cloud, (b) DSM raster, both vs the stage-04 DTM. Reports the delta.
"""
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "stockpile_volumes.py")


def outputs(cfg):
    return [common.out(cfg, "06_volumes", "volumes_summary.json")]


def _run_variant(cfg, surface, out_sub, extra_args):
    out_dir = common.out(cfg, "06_volumes", out_sub, ".keep")
    out_dir = os.path.dirname(out_dir)
    dtm = common.out(cfg, "04_rasters", "dtm_%s.tif" % common.res_suffix(cfg))
    cmd = [sys.executable, TOOL, surface, cfg["paths"]["_stockpile_boundary"],
           "--base-dtm", dtm, "--name-field", cfg["paths"]["stockpile_name_field"],
           "--out-dir", out_dir] + extra_args
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("stockpile_volumes.py failed:\n" + r.stderr[-1200:])
    csv = sorted(glob.glob(os.path.join(out_dir, "volumes_*.csv")))[-1]
    import csv as _csv
    with open(csv) as fh:
        rows = list(_csv.DictReader(fh))
    return {row["stockpile"]: float(row["volume_net_m3"]) for row in rows}, csv


def run(cfg, force=False):
    outs = outputs(cfg)
    if common.should_skip(cfg, "s06_volumes", outs, force):
        common.note_skip(cfg, "s06_volumes")
        return
    t0 = time.time()
    cloud = common.out(cfg, "03_classify", "merged_class.laz")
    dsm = common.out(cfg, "04_rasters", "dsm_%s.tif" % common.res_suffix(cfg))

    # pre-crop the cloud to the stockpile bbox (+10 m) so the volume tool
    # grids a few hundred metres instead of the whole AOI
    import geopandas as gpd
    bnd = gpd.read_file(cfg["paths"]["_stockpile_boundary"])
    if bnd.crs is not None:
        bnd = bnd.to_crs(cfg["project"]["epsg"])
    xmin, ymin, xmax, ymax = bnd.total_bounds
    pad = 10.0
    cropped = common.out(cfg, "06_volumes", "cloud_bbox.laz")
    common.run_pdal([
        {"type": "readers.las", "filename": cloud},
        {"type": "filters.crop", "bounds": "([%s, %s], [%s, %s])"
         % (xmin - pad, xmax + pad, ymin - pad, ymax + pad)},
        {"type": "writers.las", "filename": cropped, "a_srs": common.epsg_str(cfg),
         "compression": "laszip", "forward": "header,vlr"},
    ], metadata=False)

    a_vol, a_csv = _run_variant(cfg, cropped, "a_cloud",
                                ["--resolution", str(cfg["volumes"]["resolution"])])
    summary = {"variant_a_cloud": a_vol, "variant_a_csv": a_csv}
    print("volumes (a) cloud:", {k: round(v, 2) for k, v in a_vol.items()})

    if cfg["volumes"]["run_variant_b"]:
        b_vol, b_csv = _run_variant(cfg, dsm, "b_dsm", [])
        summary["variant_b_dsm"] = b_vol
        summary["variant_b_csv"] = b_csv
        summary["delta_pct"] = {}
        for k in a_vol:
            if k in b_vol and a_vol[k]:
                d = round(100 * (b_vol[k] - a_vol[k]) / a_vol[k], 2)
                summary["delta_pct"][k] = d
                print("volumes (b) dsm  %s: %.2f  (delta vs a: %+.2f%%)" %
                      (k, b_vol[k], d))

    with open(outs[0], "w") as fh:
        json.dump(summary, fh, indent=1)
    metrics = {"net_a": a_vol, "delta_pct": summary.get("delta_pct")}
    common.record_stage(cfg, "s06_volumes", time.time() - t0, metrics, outs)


if __name__ == "__main__":
    common.standalone(run)
