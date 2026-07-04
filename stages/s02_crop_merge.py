"""Stage 02 — crop each flight line to the AOI (early, in parallel) then merge
into one cloud. Cropping first keeps the merge and everything downstream small.
"""
import glob
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def outputs(cfg):
    return [common.out(cfg, "02_crop_merge", "merged_aoi.laz")]


def _crop_one(args):
    f, wkt, cfg = args
    epsg = common.epsg_str(cfg)
    tag = os.path.basename(f).split("_")[1]
    dst = common.out(cfg, "02_crop_merge", "crop", "linea_%s_aoi.laz" % tag)
    reader = {"type": "readers.las", "filename": f}
    if cfg["crop"]["override_srs"]:
        reader["override_srs"] = epsg
    common.run_pdal([
        reader,
        {"type": "filters.crop", "polygon": wkt},
        {"type": "writers.las", "filename": dst, "a_srs": epsg,
         "compression": cfg["crop"]["compression"], "forward": cfg["crop"]["forward"]},
    ], metadata=False)
    n = json.loads(subprocess.run(["pdal", "info", dst, "--metadata"],
                                  capture_output=True, text=True).stdout)["metadata"]["count"]
    return tag, dst, n


def _count_in(f):
    return json.loads(subprocess.run(["pdal", "info", f, "--metadata"],
                                     capture_output=True, text=True).stdout)["metadata"]["count"]


def run(cfg, force=False):
    outs = outputs(cfg)
    if common.should_skip(cfg, "s02_crop_merge", outs, force):
        common.note_skip(cfg, "s02_crop_merge")
        return
    t0 = time.time()
    import geopandas as gpd

    wkt = gpd.read_file(cfg["paths"]["_aoi_buffer"]).geometry.union_all().wkt
    files = sorted(glob.glob(os.path.join(cfg["paths"]["_input_laz_dir"], "*.laz")))

    with ThreadPoolExecutor(max_workers=cfg["crop"]["max_workers"]) as ex:
        crops = list(ex.map(_crop_one, [(f, wkt, cfg) for f in files]))
    nin = sum(_count_in(f) for f in files)
    nout = sum(n for _, _, n in crops)

    merged = outs[0]
    epsg = common.epsg_str(cfg)
    stages = [{"type": "readers.las", "filename": d} for _, d, _ in crops]
    stages.append({"type": "filters.merge"})
    stages.append({"type": "writers.las", "filename": merged, "a_srs": epsg,
                   "compression": cfg["merge"]["compression"], "forward": cfg["merge"]["forward"]})
    common.run_pdal(stages, metadata=False)

    pct = round(100 * nout / nin, 2) if nin else 0
    print("crop+merge: in=%d out=%d kept=%.2f%%  ->  %s" %
          (nin, nout, pct, os.path.basename(merged)))
    common.record_stage(cfg, "s02_crop_merge", time.time() - t0,
                        {"points_in": nin, "points_aoi": nout, "pct_kept": pct}, outs)


if __name__ == "__main__":
    common.standalone(run)
