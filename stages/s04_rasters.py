"""Stage 04 — rasters on the fixed grid: DTM (idw), DSM (max), density (count),
CHM (DSM-DTM, clamped). Radii are explicit (project's core lesson). QC gate:
empty 1 m cells inside Predios -> stop and export huecos.shp.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def outputs(cfg):
    d = lambda n: common.out(cfg, "04_rasters", n)  # noqa: E731
    return [d("dtm_1m.tif"), d("dsm_1m.tif"), d("density_1m.tif"), d("chm_1m.tif")]


def _writer(cfg, dst, r):
    w = {"type": "writers.gdal", "filename": dst,
         "resolution": cfg["grid"]["resolution"], "bounds": common.bounds_str(cfg),
         "output_type": r["output_type"], "gdaldriver": "GTiff",
         "nodata": r["nodata"], "data_type": r["data_type"],
         "radius": r["radius"], "window_size": r["window_size"]}
    if "power" in r:
        w["power"] = r["power"]
    return w


def _gdal(cfg, src, dst, r):
    common.run_pdal([
        {"type": "readers.las", "filename": src},
        {"type": "filters.expression", "expression": r["filter"]},
        _writer(cfg, dst, r),
    ], metadata=False)


def run(cfg, force=False):
    outs = outputs(cfg)
    if common.should_skip(cfg, "s04_rasters", outs, force):
        common.note_skip(cfg, "s04_rasters")
        return
    t0 = time.time()
    import numpy as np
    import rasterio
    import geopandas as gpd
    from rasterio import features
    from scipy import ndimage

    src = common.out(cfg, "03_classify", "merged_class.laz")
    dtm_p, dsm_p, den_p, chm_p = outs
    rr = cfg["rasters"]

    _gdal(cfg, src, dtm_p, rr["dtm"])
    if rr["dsm"]["filter"] == rr["density"]["filter"]:
        # same filter -> one read, two chained writers
        common.run_pdal([
            {"type": "readers.las", "filename": src},
            {"type": "filters.expression", "expression": rr["dsm"]["filter"]},
            _writer(cfg, dsm_p, rr["dsm"]),
            _writer(cfg, den_p, rr["density"]),
        ], metadata=False)
    else:
        _gdal(cfg, src, dsm_p, rr["dsm"])
        _gdal(cfg, src, den_p, rr["density"])

    # CHM = DSM - DTM, clamped
    nod = cfg["grid"]["nodata"]
    with rasterio.open(dsm_p) as d:
        dsm = d.read(1); prof = d.profile
    with rasterio.open(dtm_p) as d:
        dtm = d.read(1)
    valid = (dsm != nod) & (dtm != nod)
    chm = np.full(dsm.shape, nod, dtype="float32")
    cmin, cmax = rr["chm"]["clamp_min"], rr["chm"]["clamp_max"]
    chm[valid] = np.clip(dsm[valid] - dtm[valid], cmin, cmax)
    n_clamped = int((dsm[valid] - dtm[valid] > cmax).sum())
    prof.update(dtype="float32", nodata=rr["chm"]["nodata"], count=1, compress="deflate")
    with rasterio.open(chm_p, "w", **prof) as dst:
        dst.write(chm, 1)

    # hillshades (QC/visual)
    hs = rr["hillshade"]
    for s, dname in [(dtm_p, "dtm_hillshade.tif"), (dsm_p, "dsm_hillshade.tif")]:
        r = subprocess.run(["gdaldem", "hillshade", s, common.out(cfg, "04_rasters", dname),
                            "-z", str(hs["z_factor"]), "-az", str(hs["azimuth"]),
                            "-alt", str(hs["altitude"]), "-compute_edges"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("gdaldem hillshade failed (%s):\n%s"
                               % (dname, r.stderr[-1200:]))

    # ---- empty-cell QC on the density grid, masked to Predios
    # (density_real now comes from the stage-03 chunked pass)
    predios = gpd.read_file(cfg["paths"]["_predios"])
    with rasterio.open(den_p) as ds:
        dens = ds.read(1); transform = ds.transform; crs = ds.crs
    mask = features.rasterize([(g, 1) for g in predios.to_crs(crs).geometry],
                              out_shape=dens.shape, transform=transform,
                              fill=0, dtype="uint8").astype(bool)
    empty = mask & (dens == 0)
    n_empty = int(empty.sum())
    print("rasters: chm_clamped=%d  empty_cells=%d" % (n_clamped, n_empty))

    if n_empty > cfg["qc"]["max_empty_cells"]:
        lbl, nlab = ndimage.label(empty)
        from shapely.geometry import Point
        holes = []
        for i in range(1, nlab + 1):
            ys, xs = np.where(lbl == i)
            cx, cy = transform * (xs.mean() + 0.5, ys.mean() + 0.5)
            holes.append({"area_m2": len(xs), "geom": Point(cx, cy)})
        hp = common.out(cfg, "04_rasters", "huecos.shp")
        gpd.GeoDataFrame({"area_m2": [h["area_m2"] for h in holes]},
                         geometry=[h["geom"] for h in holes], crs=crs).to_file(hp)
        raise common.QCFailure("%d empty cells inside Predios (%d holes) -> %s"
                               % (n_empty, nlab, hp))

    common.record_stage(cfg, "s04_rasters", time.time() - t0,
                        {"chm_clamped_cells": n_clamped, "empty_cells": n_empty}, outs)


if __name__ == "__main__":
    common.standalone(run)
