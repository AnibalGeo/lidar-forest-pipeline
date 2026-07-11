#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stages/s07_tree_detection.py
============================
Etapa s07 — Detección y conteo de árboles individuales (vía GEOMÉTRICA).

NO es ML: es el baseline determinista sobre el CHM (máximos locales de ventana
variable + segmentación de copa por watershed). El componente ML es s08 (DeepForest);
la fusión validada es s09. Ver SPEC_deteccion_arboles.md.

Diseño alineado al pipeline base (github.com/AnibalGeo/lidar-forest-pipeline):
  - Todo parámetro que afecta una salida es explícito (no defaults silenciosos).
  - Idempotente: re-correr produce el mismo resultado.
  - QC gate: densidad implausible => warning en las métricas (no aborta duro en v1).
  - Escribe métricas para el manifest.
  - Solo LEE de out/; no toca s01–s06.

Modos de ejecución (v1, para depurar incremental):
  A) Rápido, sobre un CHM ya existente (p.ej. el chm_1m.tif de s04):
       python stages/s07_tree_detection.py --chm ".../out/04_rasters/chm_1m.tif" \
              --out-dir ".../out/07_trees" --epsg 32718
  B) Completo, generando un CHM de detección a 0.5 m desde la nube clasificada:
       python stages/s07_tree_detection.py \
              --merged-class ".../out/03_classify/merged_class.laz" \
              --dtm ".../out/04_rasters/dtm_1m.tif" \
              --out-dir ".../out/07_trees" --epsg 32718 --det-res 0.5

Dependencias Python: numpy, rasterio, scipy, scikit-image, geopandas, shapely.
Dependencia externa: PDAL CLI (solo modo B). conda install -c conda-forge pdal
"""

from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

# Los imports pesados (numpy, rasterio, scipy, skimage, geopandas) son lazy
# (dentro de cada función): la etapa es opcional y run_pipeline debe poder
# importar este módulo aunque scikit-image no esté instalado.

# --------------------------------------------------------------------------- #
# Parámetros por defecto — PUNTO DE PARTIDA para bosque mixto. Calibrar mirando
# resultados. Estos mismos valores viven en el bloque `trees:` del YAML del
# proyecto; el CLI permite sobrescribirlos para depurar rápido.
# --------------------------------------------------------------------------- #
DEFAULTS = dict(
    det_res=0.5,          # resolución del CHM de detección (m)
    det_radius=0.3536,    # radio writers.gdal para DSM 0.5 m (= res*sqrt(2)/2)
    clamp_min=0.0,        # CHM negativo -> 0
    clamp_max=50.0,       # recorta espiga-artefacto del DSM
    smooth_sigma=1.0,     # suavizado gaussiano del CHM antes de buscar picos
    min_height_m=2.0,     # altura mínima de árbol (descarta sotobosque)
    lmf_ws_a=1.2,         # ventana variable: ws_m = a + b*h  (diámetro, m)
    lmf_ws_b=0.06,
    lmf_ws_min=1.5,       # ventana mínima (m)
    epsg=32718,
    qc_min_density=50.0,  # árboles/ha plausibles (dosel) — límite inferior
    qc_max_density=3000.0,# límite superior
)


# --------------------------------------------------------------------------- #
# 1. Generación del CHM de detección (modo B)
# --------------------------------------------------------------------------- #
def _run_pdal(pipeline: dict) -> None:
    """Ejecuta un pipeline PDAL (dict -> JSON) vía CLI."""
    pdal = shutil.which("pdal")
    if pdal is None:
        raise RuntimeError("PDAL CLI no encontrado en PATH. conda install -c conda-forge pdal")
    proc = subprocess.run(
        [pdal, "pipeline", "--stdin"],
        input=json.dumps(pipeline), text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PDAL falló:\n{proc.stderr}")


def build_dsm_from_cloud(merged_class: Path, out_dsm: Path, res: float,
                         radius: float, epsg: int) -> None:
    """
    DSM = máximo retorno por celda, EXCLUYENDO ruido (Classification 7).
    Filosofía de s04: sin relleno, solo retornos reales (window_size=0).
    """
    out_dsm.parent.mkdir(parents=True, exist_ok=True)
    pipeline = {
        "pipeline": [
            {"type": "readers.las", "filename": merged_class.as_posix(),
             "override_srs": f"EPSG:{epsg}"},
            {"type": "filters.range", "limits": "Classification![7:7]"},  # sin ruido
            {"type": "writers.gdal",
             "filename": out_dsm.as_posix(),
             "output_type": "max",
             "resolution": res,
             "radius": radius,
             "window_size": 0,
             "gdaldriver": "GTiff",
             "nodata": -9999,
             "override_srs": f"EPSG:{epsg}"},
        ]
    }
    _run_pdal(pipeline)


def resample_to_match(src_path: Path, ref_profile: dict):
    """Resamplea un raster (p.ej. dtm_1m) a la grilla del DSM de detección."""
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling
    dst = np.full((ref_profile["height"], ref_profile["width"]), np.nan, dtype="float32")
    with rasterio.open(src_path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=ref_profile["transform"], dst_crs=ref_profile["crs"],
            resampling=Resampling.bilinear,
        )
    return dst


def compute_chm(dsm, dtm, clamp_min: float, clamp_max: float):
    """CHM = DSM - DTM, con clamp. NaN donde no hay DSM."""
    import numpy as np
    chm = dsm - dtm
    chm = np.where(np.isnan(dsm), np.nan, chm)
    chm = np.clip(chm, clamp_min, clamp_max)
    return chm.astype("float32")


# --------------------------------------------------------------------------- #
# 2. Detección de cimas (máximos locales de ventana variable)
# --------------------------------------------------------------------------- #
def detect_treetops(chm, pixel_size: float, p: dict):
    """
    Devuelve (rows, cols, heights) de las cimas detectadas.
    Ventana variable: árboles altos => radio de supresión mayor. Implementado como
    NMS greedy con radio dependiente de la altura (equivalente a lmf variable).
    """
    import numpy as np
    from scipy import ndimage as ndi
    from scipy.spatial import cKDTree
    chm_s = ndi.gaussian_filter(np.nan_to_num(chm, nan=0.0), sigma=p["smooth_sigma"])

    # candidatos: máximos locales en 3x3 por sobre la altura mínima
    mx = ndi.maximum_filter(chm_s, size=3, mode="constant", cval=0.0)
    cand = (chm_s == mx) & (chm_s >= p["min_height_m"])
    rows, cols = np.where(cand)
    if rows.size == 0:
        return np.array([]), np.array([]), np.array([]), chm_s

    heights = chm_s[rows, cols]
    order = np.argsort(-heights)                 # más altos primero
    rows, cols, heights = rows[order], cols[order], heights[order]

    coords = np.column_stack([rows, cols]).astype(float)
    tree = cKDTree(coords)
    taken = np.zeros(rows.size, dtype=bool)
    keep = []
    for i in range(rows.size):
        if taken[i]:
            continue
        keep.append(i)
        ws_m = max(p["lmf_ws_min"], p["lmf_ws_a"] + p["lmf_ws_b"] * heights[i])
        radius_px = (ws_m / 2.0) / pixel_size
        for j in tree.query_ball_point(coords[i], r=radius_px):
            if j != i:
                taken[j] = True          # suprime vecinos más bajos dentro de la copa
        taken[i] = True
    keep = np.array(keep, dtype=int)
    return rows[keep], cols[keep], heights[keep], chm_s


# --------------------------------------------------------------------------- #
# 3. Segmentación de copa (watershed)
# --------------------------------------------------------------------------- #
def segment_crowns(chm_s, rows, cols, min_height: float):
    """Watershed sobre -CHM; marcadores = cimas. Devuelve labels (0 = fondo)."""
    import numpy as np
    from skimage.segmentation import watershed
    markers = np.zeros(chm_s.shape, dtype=np.int32)
    for idx, (r, c) in enumerate(zip(rows, cols), start=1):
        markers[r, c] = idx
    mask = chm_s >= min_height
    labels = watershed(-chm_s, markers=markers, mask=mask)
    return labels


def crowns_to_gdf(labels, transform, crs, heights):
    """Poligoniza las copas (una geometría por árbol) -> GeoDataFrame."""
    import numpy as np
    import rasterio.features
    import geopandas as gpd
    from shapely.geometry import shape as shapely_shape
    from shapely.ops import unary_union
    geoms_by_label: dict[int, list] = {}
    for geom, val in rasterio.features.shapes(labels, mask=labels > 0, transform=transform):
        v = int(val)
        geoms_by_label.setdefault(v, []).append(shapely_shape(geom))
    recs = []
    for lbl, parts in geoms_by_label.items():
        poly = unary_union(parts)
        recs.append({"tree_id": lbl, "crown_area_m2": poly.area,
                     "crown_diam_m": 2.0 * (poly.area / np.pi) ** 0.5,
                     "height_m": float(heights[lbl - 1]), "geometry": poly})
    return gpd.GeoDataFrame(recs, crs=crs)


# --------------------------------------------------------------------------- #
# 4. Orquestación de la etapa
# --------------------------------------------------------------------------- #
def run_detection(chm_path: Path | None, merged_class: Path | None,
                  dtm_path: Path | None, out_dir: Path, params: dict) -> dict:
    import numpy as np
    import rasterio
    from rasterio.transform import xy as transform_xy
    import geopandas as gpd
    from shapely.geometry import Point

    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    p = {**DEFAULTS, **{k: v for k, v in params.items() if v is not None}}

    # --- obtener CHM de detección ---
    if chm_path is not None:
        chm_det_path = Path(chm_path)
        print(f"[s07] Usando CHM existente: {chm_det_path}")
    else:
        if merged_class is None or dtm_path is None:
            raise ValueError("Modo B requiere --merged-class y --dtm (o entrega un --chm).")
        dsm_path = out_dir / f"dsm_det_{p['det_res']}m.tif"
        chm_det_path = out_dir / f"chm_det_{p['det_res']}m.tif"
        print(f"[s07] Generando DSM {p['det_res']} m desde la nube…")
        build_dsm_from_cloud(Path(merged_class), dsm_path, p["det_res"],
                             p["det_radius"], p["epsg"])
        with rasterio.open(dsm_path) as ds:
            dsm = ds.read(1).astype("float32")
            dsm = np.where(dsm == ds.nodata, np.nan, dsm)
            ref_profile = ds.profile
        print("[s07] Resampleando DTM a la grilla de detección…")
        dtm = resample_to_match(Path(dtm_path), ref_profile)
        chm = compute_chm(dsm, dtm, p["clamp_min"], p["clamp_max"])
        prof = ref_profile.copy()
        prof.update(dtype="float32", nodata=np.nan, count=1)
        with rasterio.open(chm_det_path, "w", **prof) as dst:
            dst.write(chm, 1)
        print(f"[s07] CHM de detección escrito: {chm_det_path}")

    # --- leer CHM ---
    with rasterio.open(chm_det_path) as src:
        chm = src.read(1).astype("float32")
        if src.nodata is not None:
            chm = np.where(chm == src.nodata, np.nan, chm)
        transform = src.transform
        crs = src.crs if src.crs is not None else rasterio.crs.CRS.from_epsg(p["epsg"])
        pixel_size = abs(src.transform.a)

    # --- detección ---
    print("[s07] Detectando cimas…")
    rows, cols, heights, chm_s = detect_treetops(chm, pixel_size, p)
    n = int(rows.size)
    print(f"[s07] Cimas detectadas: {n}")

    # --- puntos de árbol ---
    if n > 0:
        xs, ys = transform_xy(transform, rows, cols, offset="center")
        pts = gpd.GeoDataFrame(
            {"tree_id": np.arange(1, n + 1),
             "height_m": heights.astype(float)},
            geometry=[Point(x, y) for x, y in zip(np.atleast_1d(xs), np.atleast_1d(ys))],
            crs=crs,
        )
        # --- copas ---
        print("[s07] Segmentando copas…")
        labels = segment_crowns(chm_s, rows, cols, p["min_height_m"])
        crowns = crowns_to_gdf(labels, transform, crs, heights)
        # adjuntar área de copa al punto (join por tree_id)
        pts = pts.merge(
            crowns[["tree_id", "crown_area_m2", "crown_diam_m"]],
            on="tree_id", how="left",
        )
    else:
        pts = gpd.GeoDataFrame({"tree_id": [], "height_m": []},
                               geometry=[], crs=crs)
        crowns = gpd.GeoDataFrame({"tree_id": []}, geometry=[], crs=crs)

    # --- escribir GeoPackage (2 capas) ---
    gpkg = out_dir / "trees_geom.gpkg"
    if gpkg.exists():
        gpkg.unlink()  # idempotencia: reescribe limpio
    if n > 0:
        pts.to_file(gpkg, layer="trees", driver="GPKG")
        crowns.to_file(gpkg, layer="crowns", driver="GPKG")
    print(f"[s07] GeoPackage: {gpkg}")

    # --- QC + métricas ---
    canopy_area_ha = float(np.nansum(chm_s >= p["min_height_m"]) * pixel_size ** 2 / 1e4)
    density = (n / canopy_area_ha) if canopy_area_ha > 0 else 0.0
    qc_ok = p["qc_min_density"] <= density <= p["qc_max_density"] if n > 0 else False
    metrics = {
        "stage": "s07_tree_detection",
        "method": "geometric_chm_localmaxima",
        "n_trees": n,
        "mean_height_m": float(np.mean(heights)) if n else None,
        "median_height_m": float(np.median(heights)) if n else None,
        "canopy_area_ha": round(canopy_area_ha, 2),
        "density_trees_per_ha": round(density, 1),
        "chm_source": str(chm_det_path),
        "chm_resolution_m": pixel_size,
        "qc_density_ok": qc_ok,
        "qc_warning": None if qc_ok else
            f"densidad {density:.1f} fuera de [{p['qc_min_density']}, {p['qc_max_density']}] tree/ha",
        "params": {k: p[k] for k in DEFAULTS},
        "runtime_s": round(time.time() - t0, 1),
    }
    (out_dir / "trees_geom_metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False))
    print(f"[s07] Métricas: {json.dumps(metrics, ensure_ascii=False, indent=2)}")
    if not qc_ok and n > 0:
        print(f"[s07][QC][WARN] {metrics['qc_warning']}", file=sys.stderr)
    return metrics


# --------------------------------------------------------------------------- #
# 4b. Interfaz estándar del pipeline (etapa opcional, ver trees: en el config)
# --------------------------------------------------------------------------- #
def outputs(cfg):
    return [common.out(cfg, "07_trees", "trees_geom.gpkg"),
            common.out(cfg, "07_trees", "trees_geom_metrics.json")]


def run(cfg, force=False):
    outs = outputs(cfg)
    if common.should_skip(cfg, "s07_tree_detection", outs, force):
        common.note_skip(cfg, "s07_tree_detection")
        return
    t0 = time.time()
    t = cfg["trees"]
    params = {k: t[k] for k in DEFAULTS if k in t}
    params["epsg"] = cfg["project"]["epsg"]
    metrics = run_detection(
        chm_path=None,
        merged_class=Path(common.out(cfg, "03_classify", "merged_class.laz")),
        dtm_path=Path(common.out(cfg, "04_rasters",
                                 "dtm_%s.tif" % common.res_suffix(cfg))),
        out_dir=Path(os.path.dirname(outs[0])),
        params=params)
    common.record_stage(cfg, "s07_tree_detection", time.time() - t0,
                        {"n_trees": metrics["n_trees"],
                         "density_trees_per_ha": metrics["density_trees_per_ha"],
                         "mean_height_m": metrics["mean_height_m"],
                         "qc_density_ok": metrics["qc_density_ok"],
                         "qc_warning": metrics["qc_warning"]}, outs)


# --------------------------------------------------------------------------- #
# 5. CLI
# --------------------------------------------------------------------------- #
def _parse_args():
    ap = argparse.ArgumentParser(description="s07 — detección geométrica de árboles")
    ap.add_argument("--chm", type=Path, help="CHM existente (modo A, rápido)")
    ap.add_argument("--merged-class", type=Path, help="nube clasificada (modo B)")
    ap.add_argument("--dtm", type=Path, help="DTM a resamplear (modo B)")
    ap.add_argument("--out-dir", type=Path, required=True)
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=type(v), default=None,
                        help=f"(default {v})")
    return ap.parse_args()


if __name__ == "__main__":
    a = _parse_args()
    params = {k: getattr(a, k) for k in DEFAULTS}
    run_detection(chm_path=a.chm, merged_class=a.merged_class, dtm_path=a.dtm,
                  out_dir=a.out_dir, params=params)
