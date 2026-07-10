"""Stage 03 — classify: statistical outlier -> noise (7), then SMRF -> ground (2).
ELM is intentionally not applied (see README). QC gate on noise% and ground%.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def outputs(cfg):
    return [common.out(cfg, "03_classify", "merged_class.laz")]


def run(cfg, force=False):
    outs = outputs(cfg)
    if common.should_skip(cfg, "s03_classify", outs, force):
        common.note_skip(cfg, "s03_classify")
        return
    t0 = time.time()
    src = common.out(cfg, "02_crop_merge", "merged_aoi.laz")
    dst = outs[0]
    o, s = cfg["classify"]["outlier"], cfg["classify"]["smrf"]
    epsg = common.epsg_str(cfg)

    common.run_pdal([
        {"type": "readers.las", "filename": src},
        {"type": "filters.outlier", "method": o["method"],
         "mean_k": o["mean_k"], "multiplier": o["multiplier"], "class": o["class"]},
        {"type": "filters.smrf", "ignore": s["ignore"], "slope": s["slope"],
         "window": s["window"], "threshold": s["threshold"], "scalar": s["scalar"],
         "cell": s["cell"]},
        {"type": "writers.las", "filename": dst, "a_srs": epsg,
         "compression": "laszip", "forward": "header,vlr"},
    ], metadata=False)

    # QC histogram + real density inside Predios, one chunked pass (no full load)
    import laspy
    import numpy as np
    import geopandas as gpd
    import shapely

    predios = gpd.read_file(cfg["paths"]["_predios"])
    pred_geom = predios.geometry.union_all()
    shapely.prepare(pred_geom)
    pred_area = float(predios.geometry.area.sum())
    pxmin, pymin, pxmax, pymax = pred_geom.bounds

    total = ground = noise = n_pred = 0
    noise_cls = o["class"]
    with laspy.open(dst) as rd:
        for pts in rd.chunk_iterator(5_000_000):
            cls = np.asarray(pts.classification)
            total += cls.size
            ground += int((cls == 2).sum())
            noise += int((cls == noise_cls).sum())
            x = np.asarray(pts.x); y = np.asarray(pts.y)
            m = ((cls != noise_cls) & (x >= pxmin) & (x <= pxmax)
                 & (y >= pymin) & (y <= pymax))
            if m.any():
                n_pred += int(shapely.contains_xy(pred_geom, x[m], y[m]).sum())
    ground_pct = round(100 * ground / total, 2)
    noise_pct = round(100 * noise / total, 2)
    density_real = round(n_pred / pred_area, 2)
    print("classify: total=%d ground=%.2f%% noise=%.2f%%  density_real=%.2f pts/m2"
          % (total, ground_pct, noise_pct, density_real))

    q = cfg["qc"]
    if noise_pct > q["noise_pct_max"]:
        raise common.QCFailure("noise %.2f%% > %.2f%%" % (noise_pct, q["noise_pct_max"]))
    if not (q["ground_pct_min"] <= ground_pct <= q["ground_pct_max"]):
        raise common.QCFailure("ground %.2f%% outside [%s, %s]%%" %
                               (ground_pct, q["ground_pct_min"], q["ground_pct_max"]))

    common.record_stage(cfg, "s03_classify", time.time() - t0,
                        {"total_points": total, "ground_pct": ground_pct,
                         "noise_pct": noise_pct, "density_real": density_real}, outs)


if __name__ == "__main__":
    common.standalone(run)
