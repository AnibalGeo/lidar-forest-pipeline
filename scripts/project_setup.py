"""Lógica reutilizable de creación de proyectos — sin I/O de consola.

Usado por scripts/new_project.py (wizard de consola) y gui/pipeline_gui.py.
Cubre: validación de rutas/capas, conteo de LAZ, detección de EPSG del AOI,
cálculo de grid.bounds, perfiles, y escritura del YAML desde template.yaml.
"""
import glob
import math
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE_DIR = os.path.dirname(HERE)
CONFIGS_DIR = os.path.join(PIPE_DIR, "configs")
TEMPLATE = os.path.join(CONFIGS_DIR, "template.yaml")

# perfil -> (resolución default, smrf overrides, qc overrides)
PROFILES = {
    "forestal": (1.0,
                 {"slope": 0.2, "threshold": 0.45},
                 {"noise_pct_max": 3.0, "ground_pct_min": 10.0, "ground_pct_max": 50.0}),
    "agro": (1.0,
             {"slope": 0.1, "threshold": 0.35},
             {"noise_pct_max": 3.0, "ground_pct_min": 20.0, "ground_pct_max": 70.0}),
    "acopios": (0.25,
                {"slope": 0.2, "threshold": 0.45},
                {"noise_pct_max": 5.0, "ground_pct_min": 2.0, "ground_pct_max": 95.0}),
}


def config_path(name):
    return os.path.join(CONFIGS_DIR, "%s.yaml" % name)


def list_configs():
    """configs/*.yaml existentes, excluyendo template.yaml. Rutas absolutas."""
    return sorted(p for p in glob.glob(os.path.join(CONFIGS_DIR, "*.yaml"))
                  if os.path.basename(p) != "template.yaml")


def scan_laz(laz_dir):
    """(lista de *.laz, tamaño total en GB) de una carpeta."""
    files = glob.glob(os.path.join(laz_dir, "*.laz"))
    gb = sum(os.path.getsize(f) for f in files) / 1024**3
    return files, gb


def validate_vector(path):
    """Capa shp/gpkg legible con geopandas; devuelve la ruta absoluta.
    Lanza FileNotFoundError si no existe, o la excepción de geopandas si no se lee.
    """
    import geopandas as gpd
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise FileNotFoundError("no existe: %s" % path)
    gpd.read_file(path, rows=1)
    return os.path.abspath(path)


def detect_epsg(aoi_path):
    """(epsg, nombre del CRS) declarados por la capa, o (None, None)."""
    import geopandas as gpd
    gdf = gpd.read_file(aoi_path)
    if gdf.crs is None:
        return None, None
    return gdf.crs.to_epsg(), gdf.crs.name


def validate_epsg(epsg):
    """EPSG como int válido según pyproj; lanza si no lo es."""
    from pyproj import CRS
    epsg = int(epsg)
    CRS.from_epsg(epsg)
    return epsg


def compute_bounds(aoi_path, epsg, res):
    """grid.bounds [xmin, xmax, ymin, ymax] desde el extent del AOI
    (reproyectado a epsg si difiere), redondeado hacia afuera a la resolución.
    """
    import geopandas as gpd
    gdf = gpd.read_file(aoi_path)
    if gdf.crs is not None and gdf.crs.to_epsg() != epsg:
        gdf = gdf.to_crs(epsg)
    bx0, by0, bx1, by1 = gdf.total_bounds
    return [math.floor(bx0 / res) * res, math.ceil(bx1 / res) * res,
            math.floor(by0 / res) * res, math.ceil(by1 / res) * res]


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


def format_bounds(bounds):
    return "[%s]" % ", ".join(fmt_num(b) for b in bounds)


# --------------------------------------------------------------- escritura
def write_config(cfg_path, name, epsg, root, laz_dir, aoi, predios,
                 uso, boundary, ortho, profile, res, bounds):
    """Escribe el YAML del proyecto desde template.yaml. uso/boundary/ortho
    pueden ser None (quedan como PENDIENTE / omitidos). Devuelve cfg_path.
    """
    smrf, qc = PROFILES[profile][1], PROFILES[profile][2]
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
    set_value(lines, ("grid", "bounds"), format_bounds(bounds))
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
              (profile, "" if profile == "forestal"
               else " (NO validado: revisar hillshade s03 antes de confiar en los números)")]
    if ortho:
        header.append("# ortho: %s   # insumo futuro de s08 (DeepForest); el pipeline aún no lo usa"
                      % yaml_path(rel_or_abs(ortho, root)))
    lines = header + lines

    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return cfg_path
