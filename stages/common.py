"""Shared infrastructure for every pipeline stage.

Handles: config loading + path resolution, config hashing, tool-version capture,
running PDAL pipelines, per-stage idempotency state, and the run manifest.
Kept in one module because all six stages use all of it.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------- config / paths
def load_config(config_path):
    """Load YAML, resolve every path, attach the config hash. Returns a dict."""
    config_path = os.path.abspath(config_path)
    with open(config_path, "rb") as fh:
        raw = fh.read()
    cfg = yaml.safe_load(raw)
    cfg["_config_path"] = config_path
    cfg["_config_sha256"] = hashlib.sha256(raw).hexdigest()

    cfg_dir = os.path.dirname(config_path)
    root = cfg["paths"]["project_root"]
    root = root if os.path.isabs(root) else os.path.normpath(os.path.join(cfg_dir, root))
    cfg["_project_root"] = root

    def resolve(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(root, p))

    p = cfg["paths"]
    for key in ("input_laz_dir", "aoi_buffer", "predios", "uso",
                "stockpile_boundary"):
        p["_" + key] = resolve(p[key])
    # Outputs ALWAYS live under {project_root}/out/ — never next to the code and
    # never configurable, so a new project cannot accidentally overwrite another.
    p["_out_dir"] = os.path.join(root, "out")
    os.makedirs(p["_out_dir"], exist_ok=True)
    return cfg


class ConfigError(Exception):
    """Raised by validate_config before any processing starts."""


def validate_config(cfg):
    """Fail fast: check inputs exist, EPSG is valid, and the AOI actually
    overlaps the LAZ. Raises ConfigError listing every problem found.
    """
    import glob as _glob
    problems = []
    p = cfg["paths"]

    laz = sorted(_glob.glob(os.path.join(p["_input_laz_dir"], "*.laz")))
    if not os.path.isdir(p["_input_laz_dir"]):
        problems.append("input_laz_dir not found: %s" % p["_input_laz_dir"])
    elif not laz:
        problems.append("no *.laz in input_laz_dir: %s" % p["_input_laz_dir"])
    for key in ("aoi_buffer", "predios", "uso", "stockpile_boundary"):
        if not os.path.exists(p["_" + key]):
            problems.append("%s not found: %s" % (key, p["_" + key]))

    epsg = cfg.get("project", {}).get("epsg")
    try:
        from pyproj import CRS
        CRS.from_epsg(int(epsg))
    except Exception:  # noqa: BLE001
        problems.append("invalid project.epsg: %r" % epsg)

    # AOI must intersect the LAZ footprint (bbox check) — catches wrong AOI/CRS
    # before an 8-minute SMRF run produces nothing.
    if laz and os.path.exists(p["_aoi_buffer"]):
        try:
            import geopandas as gpd
            aoi = gpd.read_file(p["_aoi_buffer"])
            if aoi.crs is not None and epsg:
                aoi = aoi.to_crs(int(epsg))
            axmin, aymin, axmax, aymax = aoi.total_bounds
            lxmin = lymin = float("inf"); lxmax = lymax = float("-inf")
            for f in laz:
                md = json.loads(subprocess.run(["pdal", "info", f, "--metadata"],
                                               capture_output=True, text=True).stdout)["metadata"]
                lxmin = min(lxmin, md["minx"]); lymin = min(lymin, md["miny"])
                lxmax = max(lxmax, md["maxx"]); lymax = max(lymax, md["maxy"])
            if axmin > lxmax or axmax < lxmin or aymin > lymax or aymax < lymin:
                problems.append(
                    "AOI bbox [%.0f,%.0f,%.0f,%.0f] does not intersect LAZ bbox "
                    "[%.0f,%.0f,%.0f,%.0f] — wrong AOI or CRS?"
                    % (axmin, aymin, axmax, aymax, lxmin, lymin, lxmax, lymax))
        except Exception as e:  # noqa: BLE001
            problems.append("could not read AOI %s: %s" % (p["_aoi_buffer"], e))

    if problems:
        raise ConfigError("config validation failed:\n  - " + "\n  - ".join(problems))


def out(cfg, *parts):
    """Absolute path under out_dir, creating the parent directory."""
    path = os.path.join(cfg["paths"]["_out_dir"], *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def epsg_str(cfg):
    return "EPSG:%d" % cfg["project"]["epsg"]


def bounds_str(cfg):
    """PDAL writers.gdal bounds string: ([xmin, xmax], [ymin, ymax])."""
    xmin, xmax, ymin, ymax = cfg["grid"]["bounds"]
    return "([%s, %s], [%s, %s])" % (xmin, xmax, ymin, ymax)


# --------------------------------------------------------------- run log
class _Tee:
    """Duplica un stream de consola hacia el archivo de log de la corrida."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, s):
        self._stream.write(s)
        self._fh.write(s)
        self._fh.flush()  # visible aunque el proceso muera a mitad de etapa

    def flush(self):
        self._stream.flush()
        self._fh.flush()

    def __getattr__(self, name):  # encoding, isatty, fileno, ...
        return getattr(self._stream, name)


def start_run_log(cfg):
    """Espeja stdout+stderr a {out}/logs/run_YYYYMMDD_HHMMSS.log (espejo, no
    reemplazo: la consola sigue viendo todo). Devuelve la ruta del log, que
    queda en cfg['_log_path'] para que el manifest la referencie.
    """
    path = out(cfg, "logs", "run_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    fh = open(path, "a", encoding="utf-8", errors="replace")
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    cfg["_log_path"] = path
    return path


# --------------------------------------------------------------- tool versions
@functools.lru_cache(maxsize=1)
def tool_versions():
    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
        except Exception as e:  # noqa: BLE001
            return "unavailable: %s" % e

    pdal = ""
    for line in run(["pdal", "--version"]).splitlines():
        if "pdal" in line.lower():
            pdal = line.strip("- ").strip()
    return {
        "python": sys.version.split()[0],
        "pdal": pdal or run(["pdal", "--version"]),
        "gdal": run(["gdalinfo", "--version"]),
    }


# --------------------------------------------------------------- PDAL execution
def run_pdal(stages, metadata=True):
    """Run a PDAL pipeline (list of stage dicts). Return parsed metadata dict.

    Raises RuntimeError with the tail of stderr on failure.
    """
    md_path = None
    cmd = ["pdal", "pipeline", "--stdin"]
    if metadata:
        md_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        cmd += ["--metadata", md_path]
    r = subprocess.run(cmd, input=json.dumps({"pipeline": stages}),
                       capture_output=True, text=True)
    if r.returncode != 0:
        if md_path and os.path.exists(md_path):
            os.unlink(md_path)
        raise RuntimeError("PDAL failed:\n" + r.stderr[-1500:])
    md = {}
    if md_path:
        with open(md_path) as fh:
            md = json.load(fh)
        os.unlink(md_path)
    return md


def count_where(las_path, expression):
    """Count points in a LAS/LAZ matching a PDAL expression (via filters.stats)."""
    md = run_pdal([
        {"type": "readers.las", "filename": las_path},
        {"type": "filters.expression", "expression": expression},
        {"type": "filters.stats", "dimensions": "Classification"},
    ])
    stats = md["stages"]["filters.stats"]["statistic"]
    return int(stats[0]["count"]) if stats else 0


# --------------------------------------------------------------- idempotency
def _state_path(cfg, stage):
    return out(cfg, "state", "%s.state.json" % stage)


def should_skip(cfg, stage, outputs, force):
    """True if this stage's outputs exist and were built from THIS config."""
    if force:
        return False
    sp = _state_path(cfg, stage)
    if not os.path.exists(sp):
        return False
    with open(sp) as fh:
        state = json.load(fh)
    if state.get("config_sha256") != cfg["_config_sha256"]:
        return False
    return all(os.path.exists(o) for o in outputs)


def record_stage(cfg, stage, seconds, metrics, outputs):
    """Persist the stage state (config hash next to its outputs) + update manifest."""
    state = {
        "config_sha256": cfg["_config_sha256"],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(seconds, 2),
        "metrics": metrics,
        "outputs": [os.path.relpath(o, cfg["paths"]["_out_dir"]) for o in outputs],
    }
    with open(_state_path(cfg, stage), "w") as fh:
        json.dump(state, fh, indent=1)
    _update_manifest(cfg, stage, seconds, metrics, skipped=False)


def note_skip(cfg, stage):
    print("[skip] %s — outputs present and config unchanged" % stage)
    _update_manifest(cfg, stage, 0.0, None, skipped=True)


def _update_manifest(cfg, stage, seconds, metrics, skipped):
    mpath = out(cfg, "run_manifest.json")
    if os.path.exists(mpath):
        with open(mpath) as fh:
            man = json.load(fh)
    else:
        man = {"stages": {}, "key_metrics": {}}
    man["run_date"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    man["config_path"] = cfg["_config_path"]
    man["config_sha256"] = cfg["_config_sha256"]
    man["versions"] = tool_versions()
    if cfg.get("_log_path"):
        man["log_path"] = cfg["_log_path"]
    man["stages"][stage] = {"seconds": round(seconds, 2), "skipped": skipped,
                            "metrics": metrics}
    for k in ("density_real", "ground_pct", "noise_pct"):
        if metrics and k in metrics:
            man["key_metrics"][k] = metrics[k]
    with open(mpath, "w") as fh:
        json.dump(man, fh, indent=1)


# --------------------------------------------------------------- QC failure
class QCFailure(Exception):
    """Raised when a QC gate is breached; stops the pipeline with a message."""


# --------------------------------------------------------------- standalone CLI
def standalone(run_fn):
    """Entry point for `python stages/sXX.py --config config.yaml [--force]`."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--force", action="store_true", help="ignore idempotency, rebuild")
    args = ap.parse_args()
    cfg = load_config(args.config)
    start_run_log(cfg)
    try:
        validate_config(cfg)
    except ConfigError as e:
        print("\n[CONFIG ERROR] %s" % e)
        sys.exit(3)
    t0 = time.time()
    try:
        run_fn(cfg, force=args.force)
    except QCFailure as e:
        print("\n[QC STOP] %s" % e)
        sys.exit(2)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    print("done in %.1fs" % (time.time() - t0))
