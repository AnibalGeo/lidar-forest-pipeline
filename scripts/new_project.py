"""Asistente de proyecto nuevo — genera configs/<nombre>.yaml desde template.yaml.

    python scripts/new_project.py

Wizard de consola sin dependencias nuevas (usa las del pipeline: geopandas para
validar capas). NO modifica el pipeline: solo escribe el config. Cada valor es
editable a mano después; el YAML generado conserva los comentarios del template.
La lógica (validación, EPSG, bounds, perfiles, escritura) vive en
scripts/project_setup.py; este archivo solo hace los prompts.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_setup as ps  # noqa: E402

# tecla del wizard -> nombre de perfil en project_setup.PROFILES
PROFILE_KEYS = {"1": "forestal", "2": "agro", "3": "acopios"}


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
    while True:
        p = input(label + (" [Enter para omitir]: " if optional else ": ")).strip()
        if not p and optional:
            return None
        p = os.path.expanduser(p)
        if not os.path.exists(p):
            print("  [!] no existe: %s" % p)
            continue
        try:
            return ps.validate_vector(p)
        except Exception as e:  # noqa: BLE001
            print("  [!] no se pudo leer con geopandas: %s" % e)


# --------------------------------------------------------------------- main
def main():
    print("=== Nuevo proyecto — generador de config ===\n")

    # 1. nombre
    while True:
        name = ask("Nombre del proyecto")
        if not name or " " in name:
            print("  [!] requerido, sin espacios")
            continue
        cfg_path = ps.config_path(name)
        if os.path.exists(cfg_path) and not ask_yn(
                "  ya existe %s, ¿sobrescribir?" % os.path.basename(cfg_path), "n"):
            continue
        break

    # 2. project_root
    root = ask_dir("project_root (raíz de datos del proyecto)")

    # 3. carpeta LAZ
    while True:
        laz_dir = ask_dir("Carpeta LAZ", ps.find_laz_dir(root))
        laz, gb = ps.scan_laz(laz_dir)
        if laz:
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
    detected, crs_name = ps.detect_epsg(aoi)
    if detected:
        print("EPSG detectado del AOI: %d (%s)" % (detected, crs_name))
    else:
        print("  [!] el AOI no declara CRS; ingresa el EPSG a mano")
    while True:
        try:
            epsg = ps.validate_epsg(ask("EPSG", detected))
            break
        except Exception:  # noqa: BLE001
            print("  [!] EPSG inválido")

    # 10. perfil
    print("\nPerfil:  1) forestal (VALIDADO)   2) agro   3) acopios")
    print("  [!] solo 'forestal' está validado; agro/acopios son puntos de partida")
    while True:
        prof_key = ask("Perfil", "1")
        if prof_key in PROFILE_KEYS:
            break
        print("  [!] elige 1, 2 o 3")
    prof_name = PROFILE_KEYS[prof_key]
    prof_res = ps.PROFILES[prof_name][0]

    # 11. resolución
    while True:
        try:
            res = float(ask("Resolución (m/píxel)", ps.fmt_num(prof_res)))
            assert res > 0
            break
        except Exception:  # noqa: BLE001
            print("  [!] número > 0")

    # 12. grid.bounds desde el extent del AOI, redondeado a la resolución
    bounds = ps.compute_bounds(aoi, epsg, res)
    print("grid.bounds calculado del AOI (redondeado a %s m): %s"
          % (ps.fmt_num(res), ps.format_bounds(bounds)))
    if not ask_yn("¿Usar estos bounds? (editables a mano después)", "s"):
        while True:
            raw = ask("bounds xmin,xmax,ymin,ymax")
            try:
                bounds = [float(v) for v in raw.replace("[", "").replace("]", "").split(",")]
                assert len(bounds) == 4
                break
            except Exception:  # noqa: BLE001
                print("  [!] cuatro números separados por coma")

    # ------------------------------------------------------------- escribir YAML
    ps.write_config(cfg_path, name, epsg, root, laz_dir, aoi, predios,
                    uso, boundary, ortho, prof_name, res, bounds)

    # ------------------------------------------------------------------ resumen
    print("\n=== Config escrito: %s ===" % cfg_path)
    print("  proyecto  %s   perfil %s   epsg %d   res %s m"
          % (name, prof_name, epsg, ps.fmt_num(res)))
    print("  laz       %s (%d archivos)" % (laz_dir, len(laz)))
    print("  aoi       %s" % aoi)
    print("  predios   %s" % predios)
    print("  uso       %s" % (uso or "PENDIENTE (requerido por s05)"))
    print("  acopio    %s" % (boundary or "PENDIENTE (requerido por s06)"))
    if ortho:
        print("  ortho     %s (comentado, s08 futuro)" % ortho)
    print("  bounds    %s" % ps.format_bounds(bounds))
    if not uso or not boundary:
        print("  [!] hay rutas PENDIENTES: la validación del pipeline fallará hasta completarlas")

    cmd = [sys.executable, os.path.join(ps.PIPE_DIR, "run_pipeline.py"),
           "--config", cfg_path, "--all", "--dry-run"]
    print("\nComando para la próxima vez:\n  %s" % " ".join(cmd))
    if ask_yn("¿Lanzar el dry-run ahora?", "s"):
        subprocess.call(cmd)


if __name__ == "__main__":
    main()
