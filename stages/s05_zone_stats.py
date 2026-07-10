"""Stage 05 — per-polygon canopy statistics over the CHM (mean, p95, max,
% cover above threshold), for the Uso land-use layer. Writes CSV + GPKG.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def outputs(cfg):
    return [common.out(cfg, "05_zone_stats", "uso_stats.csv"),
            common.out(cfg, "05_zone_stats", "uso_stats.gpkg")]


def run(cfg, force=False):
    outs = outputs(cfg)
    if common.should_skip(cfg, "s05_zone_stats", outs, force):
        common.note_skip(cfg, "s05_zone_stats")
        return
    t0 = time.time()
    import numpy as np
    import pandas as pd
    import rasterio
    import geopandas as gpd
    from rasterio.mask import mask as rmask

    chm = common.out(cfg, "04_rasters", "chm_1m.tif")
    thr = cfg["zone_stats"]["cover_threshold_m"]
    pct = cfg["zone_stats"]["percentile"]
    passthrough = cfg["zone_stats"]["passthrough_fields"]
    ds = rasterio.open(chm)
    nod = ds.nodata
    uso = gpd.read_file(cfg["paths"]["_uso"]).to_crs(ds.crs)

    rows = []
    for idx, row in uso.iterrows():
        try:
            arr, _ = rmask(ds, [row.geometry.__geo_interface__], crop=True,
                           filled=True, nodata=nod)
            v = arr[arr != nod]
        except Exception:  # noqa: BLE001
            v = np.array([])
        rec = {"poly_id": idx}
        for fld in passthrough:
            rec[fld] = row.get(fld)
        rec.update({"area_ha": round(row.geometry.area / 1e4, 4), "n_cells": int(v.size)})
        if v.size:
            rec.update({"chm_mean": round(float(v.mean()), 2),
                        "chm_p%d" % pct: round(float(np.percentile(v, pct)), 2),
                        "chm_max": round(float(v.max()), 2),
                        "pct_cover_gt%dm" % int(thr): round(100 * float((v > thr).mean()), 1)})
        else:
            rec.update({"chm_mean": None, "chm_p%d" % pct: None,
                        "chm_max": None, "pct_cover_gt%dm" % int(thr): None})
        rows.append(rec)

    df = pd.DataFrame(rows)
    csv_p, gpkg_p = outs
    df.to_csv(csv_p, index=False)
    stat_cols = ["area_ha", "chm_mean", "chm_p%d" % pct, "chm_max",
                 "pct_cover_gt%dm" % int(thr), "n_cells"]
    out_gdf = uso.copy()
    for c in stat_cols:
        out_gdf[c] = df.set_index("poly_id").loc[out_gdf.index, c].values
    out_gdf.to_file(gpkg_p, driver="GPKG")

    mean_global = float(df["chm_mean"].dropna().mean())
    print("zone_stats: %d polygons, mean canopy height = %.2f m" % (len(df), mean_global))
    common.record_stage(cfg, "s05_zone_stats", time.time() - t0,
                        {"n_polygons": len(df), "chm_mean_global": round(mean_global, 2)}, outs)


if __name__ == "__main__":
    common.standalone(run)
