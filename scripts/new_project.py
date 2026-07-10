"""Asistente de proyecto nuevo — genera configs/<nombre>.yaml desde template.yaml.

    python scripts/new_project.py

Wizard de consola sin dependencias nuevas (usa las del pipeline: geopandas para
validar capas). NO modifica el pipeline: solo escribe el config. Cada valor es
editable a mano después; el YAML generado conserva los comentarios del template.
"""
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE_DIR = os.path.dirname(HERE)
TEMPLATE = os.path.join(PIPE_DIR, "configs", "template.yaml")

# perfil -> (resolución default, smrf overrides, qc overrides)
PROFILES = {
    "1": ("forestal", 1.0,
          {"slope": 0.2, "threshold": 0.45},
          {"noise_pct_max": 3.0, "ground_pct_min": 10.0, "ground_pct_max": 50.0}),
    "2": ("agro", 1.0,
          {"slope": 0.1, "threshold": 0.35},
          {"noise_pct_max": 3.0, "ground_pct_min": 20.0, "ground_pct_max": 70.0}),
    "3": ("acopios", 0.25,
          {"slope": 0.2, "threshold": 0.45},
          {"noise_pct_max": 5.0, "ground_pct_min": 2.0, "ground_pct_max": 95.0}),
}


# ------------------------------------------------------------------ prompts
def ask(label, default=None):
    """input() con el default entre corchetes: 'EPSG [32718]: '."""
    sfx = " [%s]: " % default if default is not None else ": "
    raw = input(label + sfx).strip()
    return raw if raw else (str(default) if default is not None else "")


def ask_yn(label, default="s"):
    r = ask(label + " (s/n)", default).lower()
    return r.startswith("s")


def ask_dir(label, default=None):
    while True:
        p = os.path.expanduser(ask(label, default))
        if os.path.isdir(p):
            return os.path.abspath(p)
        print("  [!] no existe la carpeta: %s" % p)


def ask_vector(label, optional=False):
    """Ruta a shp/gpkg legible con geopandas. Enter para omitir si optional."""
    import geopandas as gpd
    while True:
        p = input(label + (" [Enter para omitir]: " if optional else ": ")).strip()
        if not p and optional:
            return None
        p = os.path.expanduser(p)
        if not os.path.exists(p):
            print("  [!] no existe: %s" % p)
            continue
        try:
            gpd.read_file(p, rows=1)
            return os.path.abspath(p)
        except Exception as e:  # noqa: BLE001
            print("  [!] no se pudo leer con geopandas: %s" % e)


# ------------------------------------------------------- edición del template
def set_value(lines, path, value):
    """Reemplaza el valor de una clave YAML (ruta tipo ('rasters','dtm','radius'))
    en las líneas del template, conservando el comentario de la línea.
    Rastrea la jerarquía por indentación — el template no usa listas anidadas.
    """
    stack = []  # [(indent, key)]
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):(.*)$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, m.group(2)))
        if tuple(k for _, k in stack) == tuple(path):
            rest = m.group(3)
            cm = rest.find("#")
            comment = rest[cm:] if cm >= 0 else ""
            pad = " "
            if comment:
                col = len(m.group(1)) + len(m.group(2)) + 1 + cm
                pad = " " + " " * max(0, col - len(m.group(1)) - len(m.group(2)) - 2
                                      - len(str(value)))
            lines[i] = "%s%s: %s%s%s" % (m.group(1), m.group(2), value,
                                         pad if comment else "", comment)
            return True
    raise KeyError("clave no encontrada en template: %s" % ".".join(path))


def yaml_path(p):
    """Ruta como valor YAML: forward slashes; entre comillas si tiene espacios."""
    p = p.replace("\\", "/")
    return '"%s"' % p if " " in p or ":" in os.path.basename(p) else p


def rel_or_abs(p, root):
    """Relativa a project_root si es posible (misma unidad), si no absoluta."""
    try:
        rel = os.path.relpath(p, root)
        if not rel.startswith(".."):
            return rel
    except ValueError:  # otra unidad en Windows
        pass
    return p


def fmt_num(v):
    return str(int(v)) if float(v) == int(v) else str(v)


# --------------------------------------------------------------------- main
def main():
    print("=== Nuevo proyecto — generador de config ===\n")

    # 1. nombre
    while True:
        name = ask("Nombre del proyecto")
        if not name or " " in name:
            print("  [!] requerido, sin espacios")
            continue
        cfg_path = os.path.join(PIPE_DIR, "configs", "%s.yaml" % name)
        if os.path.exists(cfg_path) and not ask_yn(
                "  ya existe %s, ¿sobrescribir?" % os.path.basename(cfg_path), "n"):
            continue
        break

    # 2. project_root
    root = ask_dir("project_root (raíz de datos del proyecto)")

    # 3. carpeta LAZ
    import glob
    while True:
        laz_dir = ask_dir("Carpeta LAZ", os.path.join(root, "01_Lidar", "IN")
                          if os.path.isdir(os.path.join(root, "01_Lidar", "IN")) else None)
        laz = glob.glob(os.path.join(laz_dir, "*.laz"))
        if laz:
            gb = sum(os.path.getsize(f) for f in laz) / 1024**3
            print("  -> %d archivos, %.2f GB" % (len(laz), gb))
            break
        print("  [!] no hay *.laz en esa carpeta")

    # 4-8. capas
    aoi = ask_vector("AOI general/buffer (shp/gpkg)")
    predios = ask_vector("AOI predios (shp/gpkg)")
    uso = ask_vector("Capa de uso/rodales (shp/gpkg)", optional=True)
    boundary = ask_vector("Boundary de acopio (shp/gpkg)", optional=True)
    ortho = input("Ortomosaico (tif) [Enter para omitir]: ").strip() or None
    if ortho:
        ortho = os.path.abspath(os.path.expanduser(ortho))
        if not os.path.exists(ortho):
            print("  [!] aviso: no existe (se guarda comentado igual): %s" % ortho)

    # 9. EPSG (detectado del AOI)
    import geopandas as gpd
    aoi_gdf = gpd.read_file(aoi)
    detected = aoi_gdf.crs.to_epsg() if aoi_gdf.crs is not None else None
    if detected:
        print("EPSG detectado del AOI: %d (%s)" % (detected, aoi_gdf.crs.name))
    else:
        print("  [!] el AOI no declara CRS; ingresa el EPSG a mano")
    while True:
        try:
            epsg = int(ask("EPSG", detected))
            from pyproj import CRS
            CRS.from_epsg(epsg)
            break
        except Exception:  # noqa: BLE001
            print("  [!] EPSG inválido")

    # 10. perfil
    print("\nPerfil:  1) forestal (VALIDADO)   2) agro   3) acopios")
    print("  [!] solo 'forestal' está validado; agro/acopios son puntos de partida")
    while True:
        prof_key = ask("Perfil", "1")
        if prof_key in PROFILES:
            break
        print("  [!] elige 1, 2 o 3")
    prof_name, prof_res, smrf, qc = PROFILES[prof_key]

    # 11. resolución
    while True:
        try:
            res = float(ask("Resolución (m/píxel)", fmt_num(prof_res)))
            assert res > 0
            break
        except Exception:  # noqa: BLE001
            print("  [!] número > 0")

    # 12. grid.bounds desde el extent del AOI, redondeado a la resolución
    if aoi_gdf.crs is not None and detected != epsg:
        aoi_gdf = aoi_gdf.to_crs(epsg)
    bx0, by0, bx1, by1 = aoi_gdf.total_bounds
    bounds = [math.floor(bx0 / res) * res, math.ceil(bx1 / res) * res,
              math.floor(by0 / res) * res, math.ceil(by1 / res) * res]
    bounds_s = "[%s]" % ", ".join(fmt_num(b) for b in bounds)
    print("grid.bounds calculado del AOI (redondeado a %s m): %s" % (fmt_num(res), bounds_s))
    if not ask_yn("¿Usar estos bounds? (editables a mano después)", "s"):
        while True:
            raw = ask("bounds xmin,xmax,ymin,ymax")
            try:
                bounds = [float(v) for v in raw.replace("[", "").replace("]", "").split(",")]
                assert len(bounds) == 4
                bounds_s = "[%s]" % ", ".join(fmt_num(b) for b in bounds)
                break
            except Exception:  # noqa: BLE001
                print("  [!] cuatro números separados por coma")

    # ------------------------------------------------------------- escribir YAML
    with open(TEMPLATE, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    pend = "  # TODO: no definido en el wizard — requerido antes de correr"
    set_value(lines, ("project", "name"), name)
    set_value(lines, ("project", "epsg"), epsg)
    set_value(lines, ("paths", "project_root"), yaml_path(root))
    set_value(lines, ("paths", "input_laz_dir"), yaml_path(rel_or_abs(laz_dir, root)))
    set_value(lines, ("paths", "aoi_buffer"), yaml_path(rel_or_abs(aoi, root)))
    set_value(lines, ("paths", "predios"), yaml_path(rel_or_abs(predios, root)))
    set_value(lines, ("paths", "uso"),
              yaml_path(rel_or_abs(uso, root)) if uso else "PENDIENTE.shp" + pend + " s05")
    set_value(lines, ("paths", "stockpile_boundary"),
              yaml_path(rel_or_abs(boundary, root)) if boundary
              else "PENDIENTE.gpkg" + pend + " s06")
    set_value(lines, ("grid", "resolution"), fmt_num(res))
    set_value(lines, ("grid", "bounds"), bounds_s)
    # radios de writers.gdal recalculados SIEMPRE desde la resolución elegida
    set_value(lines, ("rasters", "dtm", "radius"), round(res * math.sqrt(2), 4))
    set_value(lines, ("rasters", "dsm", "radius"), round(res * math.sqrt(2) / 2, 4))
    set_value(lines, ("rasters", "density", "radius"), round(res * math.sqrt(2) / 2, 4))
    # perfil: smrf + gates qc
    set_value(lines, ("classify", "smrf", "slope"), smrf["slope"])
    set_value(lines, ("classify", "smrf", "threshold"), smrf["threshold"])
    for k, v in qc.items():
        set_value(lines, ("qc", k), v)
    set_value(lines, ("volumes", "resolution"), fmt_num(res))

    header = ["# Config generado por scripts/new_project.py — perfil: %s%s" %
              (prof_name, "" if prof_name == "forestal"
               else " (NO validado: revisar hillshade s03 antes de confiar en los números)")]
    if ortho:
        header.append("# ortho: %s   # insumo futuro de s08 (DeepForest); el pipeline aún no lo usa"
                      % yaml_path(rel_or_abs(ortho, root)))
    lines = header + lines

    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    # ------------------------------------------------------------------ resumen
    print("\n=== Config escrito: %s ===" % cfg_path)
    print("  proyecto  %s   perfil %s   epsg %d   res %s m" % (name, prof_name, epsg, fmt_num(res)))
    print("  laz       %s (%d archivos)" % (laz_dir, len(laz)))
    print("  aoi       %s" % aoi)
    print("  predios   %s" % predios)
    print("  uso       %s" % (uso or "PENDIENTE (requerido por s05)"))
    print("  acopio    %s" % (boundary or "PENDIENTE (requerido por s06)"))
    if ortho:
        print("  ortho     %s (comentado, s08 futuro)" % ortho)
    print("  bounds    %s" % bounds_s)
    if not uso or not boundary:
        print("  [!] hay rutas PENDIENTES: la validación del pipeline fallará hasta completarlas")

    cmd = [sys.executable, os.path.join(PIPE_DIR, "run_pipeline.py"),
           "--config", cfg_path, "--all", "--dry-run"]
    print("\nComando para la próxima vez:\n  %s" % " ".join(cmd))
    if ask_yn("¿Lanzar el dry-run ahora?", "s"):
        subprocess.call(cmd)


if __name__ == "__main__":
    main()
