"""LiDAR pipeline orchestrator. One config per project, no code edits.

    python run_pipeline.py --config configs/myproject.yaml --all       # every stage
    python run_pipeline.py --config configs/myproject.yaml --from s03  # s03 onwards
    python run_pipeline.py --config configs/myproject.yaml --only s04  # one stage
    python run_pipeline.py --config configs/myproject.yaml --all --force   # rebuild
    python run_pipeline.py --config configs/myproject.yaml --all --dry-run # plan only
    python run_pipeline.py --list-stages

--config is mandatory (no default). Stages are idempotent: an up-to-date output
built from the current config is skipped. A QC breach stops the run (exit 2);
a bad config stops before any processing (exit 3).
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stages"))

import common  # noqa: E402
import s01_inventory, s02_crop_merge, s03_classify  # noqa: E402
import s04_rasters, s05_zone_stats, s06_volumes  # noqa: E402
import s07_tree_detection  # noqa: E402

STAGES = [
    ("s01", "s01_inventory", s01_inventory.run),
    ("s02", "s02_crop_merge", s02_crop_merge.run),
    ("s03", "s03_classify", s03_classify.run),
    ("s04", "s04_rasters", s04_rasters.run),
    ("s05", "s05_zone_stats", s05_zone_stats.run),
    ("s06", "s06_volumes", s06_volumes.run),
    ("s07", "s07_tree_detection", s07_tree_detection.run),  # opcional: trees.enabled
]
ALIASES = {s[0]: i for i, s in enumerate(STAGES)}
ALIASES.update({s[1]: i for i, s in enumerate(STAGES)})


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _index(name):
    if name not in ALIASES:
        sys.exit("unknown stage %r; valid: %s" %
                 (name, ", ".join(s[0] for s in STAGES)))
    return ALIASES[name]


def _dry_run(cfg, selected):
    """Show what would run, where, and with which parameters — no execution."""
    c = cfg["classify"]; r = cfg["rasters"]; q = cfg["qc"]
    print("paths:")
    for k in ("input_laz_dir", "aoi_buffer", "predios", "uso", "stockpile_boundary"):
        print("  %-18s %s" % (k, cfg["paths"]["_" + k]))
    print("  %-18s %s" % ("out_dir", cfg["paths"]["_out_dir"]))
    print("grid: %s m, epsg %s, bounds %s" %
          (cfg["grid"]["resolution"], cfg["project"]["epsg"], cfg["grid"]["bounds"]))
    print("classify: outlier k=%s/m=%s ; SMRF slope=%s win=%s thr=%s scalar=%s cell=%s" %
          (c["outlier"]["mean_k"], c["outlier"]["multiplier"], c["smrf"]["slope"],
           c["smrf"]["window"], c["smrf"]["threshold"], c["smrf"]["scalar"], c["smrf"]["cell"]))
    print("rasters: DTM %s r=%s ws=%s ; DSM %s r=%s ws=%s ; density r=%s ; CHM clamp[%s,%s]" %
          (r["dtm"]["output_type"], r["dtm"]["radius"], r["dtm"]["window_size"],
           r["dsm"]["output_type"], r["dsm"]["radius"], r["dsm"]["window_size"],
           r["density"]["radius"], r["chm"]["clamp_min"], r["chm"]["clamp_max"]))
    print("qc gates: noise<=%s%% ground=[%s,%s]%% max_empty=%s" %
          (q["noise_pct_max"], q["ground_pct_min"], q["ground_pct_max"], q["max_empty_cells"]))
    print("\nplan:")
    import importlib
    for short, name, _ in selected:
        mod = importlib.import_module(name)
        action = "SKIP" if common.should_skip(cfg, name, mod.outputs(cfg), False) else "RUN "
        print("  [%s] %s (%s)" % (action, short, name))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="project config YAML (required unless --list-stages)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--from", dest="from_", metavar="STAGE")
    ap.add_argument("--only", metavar="STAGE")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="show plan and params, do not run")
    ap.add_argument("--list-stages", action="store_true", help="print stages and exit")
    args = ap.parse_args()

    if args.list_stages:
        for short, name, _ in STAGES:
            print("%-4s %s" % (short, name))
        return
    if not args.config:
        ap.error("--config is required")

    if args.only:
        selected = STAGES[_index(args.only):_index(args.only) + 1]
    elif args.from_:
        selected = STAGES[_index(args.from_):]
    elif args.all:
        selected = STAGES
    else:
        ap.error("choose --all, --from STAGE, or --only STAGE")

    cfg = common.load_config(args.config)
    log_path = None
    if not args.dry_run:
        log_path = common.start_run_log(cfg)
    try:
        common.validate_config(cfg)
    except common.ConfigError as e:
        print("[CONFIG ERROR] %s" % e)
        sys.exit(3)
    print("config %s  sha256=%s" % (args.config, cfg["_config_sha256"][:12]))
    print("out_dir %s" % cfg["paths"]["_out_dir"])
    if log_path:
        print("log     %s" % log_path)
    print()

    if not cfg.get("trees", {}).get("enabled", False):
        if any(s[0] == "s07" for s in selected):
            print("[off] s07_tree_detection — trees.enabled=false (o ausente) en el config\n")
        selected = [s for s in selected if s[0] != "s07"]

    if args.dry_run:
        _dry_run(cfg, selected)
        return

    t0 = time.time()
    for short, name, fn in selected:
        # banners INICIO/FIN: los parsea la GUI (gui/pipeline_gui.py) — no cambiar formato
        print("=== INICIO %s %s [%s] ===" % (short, name, _now()))
        st = time.time()
        try:
            fn(cfg, force=args.force)
        except common.QCFailure as e:
            print("\n[QC STOP @ %s] %s" % (name, e))
            sys.exit(2)
        except Exception:
            import traceback
            traceback.print_exc()
            print("\n[ERROR @ %s]%s" % (name, " — log: %s" % log_path if log_path else ""))
            sys.exit(1)
        print("=== FIN %s %s (%.1fs) ===\n" % (short, name, time.time() - st))
    print("pipeline OK — %d stage(s) in %.1fs" % (len(selected), time.time() - t0))
    print("manifest: %s" % common.out(cfg, "run_manifest.json"))
    if log_path:
        print("log: %s" % log_path)


if __name__ == "__main__":
    main()
