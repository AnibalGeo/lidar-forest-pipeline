#!/usr/bin/env python3
"""
stockpile_volumes.py — Stockpile volume computation with DUAL input:
either a point cloud (LAS/LAZ) or a photogrammetric DSM (GeoTIFF).

    Point cloud path:  LAZ/LAS -> gridded surface (highest point per cell)
    Raster path:       DSM GeoTIFF -> used as-is on its own grid
    Both converge on the same volume engine.

Base surface options (per project reality):
    --base-dtm ref.tif     volume against a reference terrain model
    --base-elev 512.3      volume above a constant elevation (e.g. slab/bench)
    (default)              base interpolated from the stockpile toe: surface
                           elevations sampled along the boundary polygon are
                           TIN-interpolated across the interior — the standard
                           method when no pre-stockpile terrain exists.

Inputs:
    surface     .las/.laz  or  .tif/.tiff (DSM)
    boundary    polygon layer (GPKG/SHP), one polygon per stockpile,
                optional name column (--name-field)

Outputs:
    volumes_<date>.csv / .xlsx    one row per stockpile: net/fill/cut volume,
                                  area, mean & max height, data coverage %
    <name>_height.tif  (optional, --save-rasters) height-above-base raster
                       for visual QC / client figures

Usage:
    python stockpile_volumes.py cloud.laz  stockpiles.gpkg
    python stockpile_volumes.py dsm.tif    stockpiles.gpkg --base-dtm terrain.tif
    python stockpile_volumes.py cloud.laz  stockpiles.gpkg --resolution 0.25 \
        --name-field nombre --save-rasters --rmse-z 0.05

Requires: numpy, scipy, rasterio, geopandas, shapely, pandas, laspy[lazrs], openpyxl

Author: Anibal Matamala (github.com/AnibalGeo) — MIT License
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio import features as rfeatures
from rasterio.transform import from_origin
from scipy.interpolate import griddata

# ------------------------------------------------------------ surface load

def surface_from_pointcloud(path: Path, resolution: float):
    """Grid a LAS/LAZ cloud: highest point per cell (surface model).

    Returns (array float32 with NaN gaps, affine transform, crs_or_None).
    """
    import laspy

    print(f"[..] Reading point cloud: {path.name}")
    las = laspy.read(path)
    x, y, z = np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)

    # Optional hygiene: drop classified noise (7, 18) if classification exists
    if hasattr(las, "classification"):
        cls = np.asarray(las.classification)
        keep = ~np.isin(cls, (7, 18))
        x, y, z = x[keep], y[keep], z[keep]

    crs = None
    try:
        crs = las.header.parse_crs()
    except Exception:
        pass

    minx, maxx = x.min(), x.max()
    miny, maxy = y.min(), y.max()
    width = int(np.ceil((maxx - minx) / resolution)) + 1
    height = int(np.ceil((maxy - miny) / resolution)) + 1
    print(f"[..] Gridding {len(x):,} pts -> {width}x{height} cells "
          f"@ {resolution} m (highest point per cell)")

    col = ((x - minx) / resolution).astype(np.int64)
    row = ((maxy - y) / resolution).astype(np.int64)
    flat = row * width + col

    grid = np.full(width * height, -np.inf, dtype=np.float64)
    np.maximum.at(grid, flat, z)                    # max Z per cell, vectorized
    grid[grid == -np.inf] = np.nan
    dsm = grid.reshape(height, width).astype(np.float32)

    transform = from_origin(minx, maxy, resolution, resolution)
    return dsm, transform, crs


def surface_from_raster(path: Path):
    """Load a DSM GeoTIFF as (array with NaN nodata, transform, crs)."""
    print(f"[..] Reading DSM raster: {path.name}")
    with rasterio.open(path) as src:
        dsm = src.read(1).astype(np.float32)
        if src.nodata is not None:
            dsm[dsm == src.nodata] = np.nan
        if abs(abs(src.transform.a) - abs(src.transform.e)) > 1e-6:
            print("[WARN] Non-square pixels; using |a*e| as cell area.")
        return dsm, src.transform, src.crs


# ------------------------------------------------------------- base surface

def base_from_boundary(dsm, transform, geom, step_px=1.0):
    """TIN-interpolate the base from surface elevations along the polygon toe."""
    res = abs(transform.a)
    ring_pts = []
    boundaries = [geom.exterior] + list(geom.interiors)
    for ring in boundaries:
        length = ring.length
        n = max(16, int(length / (res * step_px)))
        for d in np.linspace(0, length, n, endpoint=False):
            p = ring.interpolate(d)
            r, c = rasterio.transform.rowcol(transform, p.x, p.y)
            # sample a 3x3 neighborhood minimum: the toe, not pile spill-over
            r0, r1 = max(r - 1, 0), min(r + 2, dsm.shape[0])
            c0, c1 = max(c - 1, 0), min(c + 2, dsm.shape[1])
            patch = dsm[r0:r1, c0:c1]
            if np.all(np.isnan(patch)):
                continue
            ring_pts.append((p.x, p.y, np.nanmin(patch)))

    if len(ring_pts) < 3:
        raise ValueError("Not enough valid boundary samples to build a base "
                         "(is the polygon inside the data extent?)")

    pts = np.array(ring_pts)
    rows, cols = np.indices(dsm.shape)
    xs, ys = rasterio.transform.xy(transform, rows, cols)
    xi = np.column_stack([np.asarray(xs).ravel(), np.asarray(ys).ravel()])

    base = griddata(pts[:, :2], pts[:, 2], xi, method="linear")
    nearest = griddata(pts[:, :2], pts[:, 2], xi, method="nearest")
    base = np.where(np.isnan(base), nearest, base)   # fill TIN edge gaps
    return base.reshape(dsm.shape).astype(np.float32)


# ------------------------------------------------------------------- engine

def compute_stockpile(dsm, transform, crs, geom, name, args, out_dir):
    """Clip to polygon, build base, integrate volume. Returns a result dict."""
    res_x, res_y = abs(transform.a), abs(transform.e)
    cell_area = res_x * res_y

    mask = rfeatures.geometry_mask([geom.__geo_interface__],
                                   out_shape=dsm.shape,
                                   transform=transform,
                                   invert=True)
    inside = mask & ~np.isnan(dsm)
    n_mask = int(mask.sum())
    if n_mask == 0:
        print(f"[WARN] '{name}': polygon outside data extent - skipped")
        return None
    coverage = 100.0 * inside.sum() / n_mask

    # ---- base surface
    if args.base_dtm:
        with rasterio.open(args.base_dtm) as ref:
            # sample reference DTM at every cell center of our grid
            rows, cols = np.indices(dsm.shape)
            xs, ys = rasterio.transform.xy(transform, rows, cols)
            coords = np.column_stack([np.asarray(xs).ravel(),
                                      np.asarray(ys).ravel()])
            base = np.array([v[0] for v in ref.sample(coords)],
                            dtype=np.float32).reshape(dsm.shape)
            if ref.nodata is not None:
                base[base == ref.nodata] = np.nan
    elif args.base_elev is not None:
        base = np.full_like(dsm, args.base_elev)
    else:
        base = base_from_boundary(dsm, transform, geom)

    height = dsm - base
    height[~inside] = np.nan
    hv = height[inside & np.isfinite(height)]
    if hv.size == 0:
        print(f"[WARN] '{name}': no valid cells - skipped")
        return None

    fill = float(np.nansum(np.clip(hv, 0, None)) * cell_area)   # material above base
    cut = float(-np.nansum(np.clip(hv, None, 0)) * cell_area)   # below base (holes)
    area = float(inside.sum() * cell_area)
    unc = float(args.rmse_z * area) if args.rmse_z else None

    if args.save_rasters:
        prof = dict(driver="GTiff", height=dsm.shape[0], width=dsm.shape[1],
                    count=1, dtype="float32", transform=transform, crs=crs,
                    nodata=np.nan, compress="deflate", tiled=True)
        out = out_dir / f"{name}_height.tif"
        with rasterio.open(out, "w", **prof) as dst:
            dst.write(height, 1)
        print(f"     saved {out.name}")

    return {
        "stockpile": name,
        "volume_net_m3": round(fill - cut, 2),
        "volume_fill_m3": round(fill, 2),
        "volume_cut_m3": round(cut, 2),
        "area_m2": round(area, 2),
        "height_mean_m": round(float(np.nanmean(hv)), 2),
        "height_max_m": round(float(np.nanmax(hv)), 2),
        "coverage_pct": round(coverage, 1),
        "uncertainty_m3": round(unc, 1) if unc else "",
        "cell_size_m": res_x,
        "base_method": ("ref_dtm" if args.base_dtm
                        else "const_elev" if args.base_elev is not None
                        else "toe_interpolation"),
    }


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Stockpile volumes from a point cloud (LAS/LAZ) or DSM (GeoTIFF).")
    ap.add_argument("surface", type=Path, help="Input .las/.laz or .tif DSM")
    ap.add_argument("boundaries", type=Path,
                    help="Polygon layer (GPKG/SHP), one polygon per stockpile")
    ap.add_argument("--name-field", default=None,
                    help="Attribute with stockpile names (default: acopio_1..N)")
    ap.add_argument("--resolution", type=float, default=0.25,
                    help="Grid cell size in m for point-cloud input (default 0.25)")
    ap.add_argument("--base-dtm", type=Path, default=None,
                    help="Reference terrain GeoTIFF for volume base")
    ap.add_argument("--base-elev", type=float, default=None,
                    help="Constant base elevation (e.g. concrete slab)")
    ap.add_argument("--rmse-z", type=float, default=None,
                    help="Vertical RMSE (m) to report volume uncertainty = rmse*area")
    ap.add_argument("--save-rasters", action="store_true",
                    help="Write per-stockpile height-above-base GeoTIFFs")
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args()

    if args.base_dtm and args.base_elev is not None:
        sys.exit("[ERROR] Use --base-dtm OR --base-elev, not both.")

    ext = args.surface.suffix.lower()
    if ext in (".las", ".laz"):
        dsm, transform, crs = surface_from_pointcloud(args.surface, args.resolution)
    elif ext in (".tif", ".tiff"):
        dsm, transform, crs = surface_from_raster(args.surface)
    else:
        sys.exit(f"[ERROR] Unsupported input: {ext} (expected .las/.laz/.tif)")

    import geopandas as gpd
    gdf = gpd.read_file(args.boundaries)
    if crs is not None and gdf.crs is not None and gdf.crs != crs:
        print(f"[INFO] Reprojecting boundaries {gdf.crs} -> {crs}")
        gdf = gdf.to_crs(crs)
    elif crs is None:
        print("[WARN] Surface has no CRS - assuming boundaries share the same "
              "coordinate system. Verify this.")

    out_dir = args.out_dir or args.surface.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, row in gdf.iterrows():
        name = (str(row[args.name_field]) if args.name_field
                else f"acopio_{i + 1}")
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        geoms = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for j, g in enumerate(geoms):
            label = name if len(geoms) == 1 else f"{name}_{j + 1}"
            print(f"== {label} ==")
            r = compute_stockpile(dsm, transform, crs, g, label, args, out_dir)
            if r:
                results.append(r)
                print(f"     net {r['volume_net_m3']:,.1f} m3 | "
                      f"area {r['area_m2']:,.0f} m2 | "
                      f"coverage {r['coverage_pct']}%")

    if not results:
        sys.exit("[ERROR] No stockpiles computed.")

    df = pd.DataFrame(results)
    stamp = date.today().isoformat()
    csv_path = out_dir / f"volumes_{stamp}.csv"
    df.to_csv(csv_path, index=False)
    try:
        df.to_excel(out_dir / f"volumes_{stamp}.xlsx", index=False)
    except ImportError:
        print("[WARN] openpyxl not installed - CSV only")
    print(f"\n[OK] {csv_path.name} — {len(df)} stockpile(s), "
          f"total net {df['volume_net_m3'].sum():,.1f} m3")


if __name__ == "__main__":
    main()
