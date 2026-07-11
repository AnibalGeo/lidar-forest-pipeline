"""GUI de escritorio del pipeline LiDAR — solo tkinter/ttk (stdlib).

    python gui/pipeline_gui.py

Tres zonas: A) proyecto (elegir config existente o crear uno nuevo con
scripts/project_setup.py), B) ejecución (run_pipeline.py como subprocess,
salida en vivo vía hilo+queue, progreso por banners de etapa), C) log.

REGLA: la GUI no contiene lógica de pipeline ni parámetros — solo escribe/
selecciona configs y lanza el proceso. Los valores viven en el YAML.
"""
import math
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PIPE_DIR, "scripts"))
import project_setup as ps  # noqa: E402

RUN_PIPELINE = os.path.join(PIPE_DIR, "run_pipeline.py")
N_STAGES = 7  # s01..s07 (s07 opcional; la barra se completa al exit 0)

RE_INICIO = re.compile(r"^=== INICIO (s\d+) (\S+)")
RE_FIN = re.compile(r"^=== FIN (s\d+) (\S+) \(([\d.]+)s\)")
RE_LOG = re.compile(r"^log:?\s+(.+)$")
RE_OUTDIR = re.compile(r"^out_dir\s+(.+)$")

VECTOR_TYPES = [("Capas vectoriales", "*.shp *.gpkg"), ("Todos", "*.*")]


class App:
    def __init__(self, root):
        self.root = root
        root.title("Pipeline LiDAR")
        root.minsize(760, 640)
        self.proc = None
        self.q = queue.Queue()
        self.cancelled = False
        self.last_log = None
        self.last_outdir = None

        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)
        self._zone_a(main)
        self._zone_b(main)
        self._zone_c(main)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._drain_queue)

    # ------------------------------------------------------------- zona A
    def _zone_a(self, parent):
        f = ttk.LabelFrame(parent, text="A) PROYECTO", padding=6)
        f.pack(fill="x")
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Config existente:").pack(side="left")
        self.cfg_var = tk.StringVar()
        self.cfg_combo = ttk.Combobox(row, textvariable=self.cfg_var,
                                      state="readonly", width=40)
        self.cfg_combo.pack(side="left", padx=6)
        ttk.Button(row, text="Nuevo proyecto",
                   command=self._toggle_form).pack(side="left", padx=6)
        self.cfg_combo.bind("<<ComboboxSelected>>", self._load_config_into_form)
        self._refresh_configs()

        # ---- formulario de proyecto nuevo (oculto hasta pulsar el botón)
        self.form = ttk.Frame(f, padding=(4, 8, 4, 2))
        self.form_visible = False
        self._build_form(self.form)

    # ---- formulario: pestañas por nivel de parámetros -----------------------
    def _build_form(self, fm):
        self.tpl = ps.parse_template()  # {ruta: (valor, comentario)} — defaults
        for attr in ("v_name", "v_root", "v_dest", "v_laz", "v_aoi", "v_predios",
                     "v_uso", "v_acopio", "v_ortho", "v_epsg"):
            setattr(self, attr, tk.StringVar())
        self.v_perfil = tk.StringVar(value="forestal")
        self.v_trees = tk.BooleanVar(value=False)
        # (atributo, etiqueta, ruta en template.yaml) por pestaña
        self.prod_fields = [
            ("v_res", "Resolución DTM/DSM/CHM (m)", ("grid", "resolution")),
            ("v_clamp", "Clamp CHM (m)", ("rasters", "chm", "clamp_max")),
            ("v_cover", "Umbral cobertura (m)", ("zone_stats", "cover_threshold_m")),
            ("v_pctl", "Percentil", ("zone_stats", "percentile")),
            ("v_vol_res", "Resolución cubicación (m)", ("volumes", "resolution")),
        ]
        self.tree_fields = [
            ("v_t_det_res", "det_res (m)", ("trees", "det_res")),
            ("v_t_min_height", "min_height (m)", ("trees", "min_height_m")),
            ("v_t_ws_a", "lmf_ws_a", ("trees", "lmf_ws_a")),
            ("v_t_ws_b", "lmf_ws_b", ("trees", "lmf_ws_b")),
            ("v_t_ws_min", "lmf_ws_min (m)", ("trees", "lmf_ws_min")),
            ("v_t_sigma", "smooth_sigma", ("trees", "smooth_sigma")),
            ("v_t_qmin", "qc densidad min (árb/ha)", ("trees", "qc_min_density")),
            ("v_t_qmax", "qc densidad max (árb/ha)", ("trees", "qc_max_density")),
        ]
        self.adv_fields = [
            ("v_s_slope", "SMRF slope", ("classify", "smrf", "slope")),
            ("v_s_window", "SMRF window (celdas)", ("classify", "smrf", "window")),
            ("v_s_thr", "SMRF threshold (m)", ("classify", "smrf", "threshold")),
            ("v_s_scalar", "SMRF scalar", ("classify", "smrf", "scalar")),
            ("v_s_cell", "SMRF cell (m)", ("classify", "smrf", "cell")),
            ("v_o_meank", "outlier mean_k", ("classify", "outlier", "mean_k")),
            ("v_o_mult", "outlier multiplier", ("classify", "outlier", "multiplier")),
        ]
        self.param_fields = self.prod_fields + self.tree_fields + self.adv_fields
        for attr, _label, _path in self.param_fields:
            setattr(self, attr, tk.StringVar())

        nb = ttk.Notebook(fm)
        nb.pack(fill="both", expand=True)
        tabs = {}
        for key, title in (("proyecto", "Proyecto"), ("productos", "Productos"),
                           ("arboles", "Árboles s07"), ("avanzado", "Avanzado")):
            tabs[key] = ttk.Frame(nb, padding=6)
            nb.add(tabs[key], text=title)
        self._tab_proyecto(tabs["proyecto"])
        self._tab_productos(tabs["productos"])
        self._tab_arboles(tabs["arboles"])
        self._tab_avanzado(tabs["avanzado"])
        ttk.Button(fm, text="Guardar config",
                   command=self._save_config).pack(anchor="w", pady=6)
        self._load_profile_defaults("forestal")

    def _tab_proyecto(self, fm):
        def entry_row(r, label, var, browse=None, extra=None):
            ttk.Label(fm, text=label).grid(row=r, column=0, sticky="w", pady=1)
            e = ttk.Entry(fm, textvariable=var, width=52)
            e.grid(row=r, column=1, sticky="we", pady=1)
            if browse:
                ttk.Button(fm, text="Examinar...", width=11,
                           command=browse).grid(row=r, column=2, padx=3)
            if extra:
                extra.grid(row=r, column=3, sticky="w", padx=4)
            return e

        fm.columnconfigure(1, weight=1)
        entry_row(0, "Nombre", self.v_name)
        entry_row(1, "project_root", self.v_root, self._browse_root)
        self.dest_info = ttk.Label(fm, text="(default: project_root/out)",
                                   foreground="gray")
        entry_row(2, "Carpeta de destino", self.v_dest, self._browse_dest,
                  self.dest_info)
        self.laz_info = ttk.Label(fm, text="", foreground="gray")
        entry_row(3, "Carpeta LAZ", self.v_laz, self._browse_laz, self.laz_info)
        entry_row(4, "AOI buffer", self.v_aoi, self._browse_aoi)
        entry_row(5, "Predios", self.v_predios,
                  lambda: self._browse_vector(self.v_predios))
        entry_row(6, "Uso (opcional)", self.v_uso,
                  lambda: self._browse_vector(self.v_uso))
        entry_row(7, "Acopio (opcional)", self.v_acopio,
                  lambda: self._browse_vector(self.v_acopio))
        entry_row(8, "Ortomosaico (opcional)", self.v_ortho, self._browse_ortho)
        self.epsg_info = ttk.Label(fm, text="", foreground="gray")
        entry_row(9, "EPSG", self.v_epsg, extra=self.epsg_info)

        ttk.Label(fm, text="Perfil").grid(row=10, column=0, sticky="w")
        pf = ttk.Frame(fm)
        pf.grid(row=10, column=1, columnspan=3, sticky="w")
        for p in ("forestal", "agro", "acopios"):
            ttk.Radiobutton(pf, text=p, value=p, variable=self.v_perfil,
                            command=self._on_profile).pack(side="left", padx=4)
        ttk.Label(pf, text="(solo 'forestal' validado; cambiar perfil resetea las"
                           " otras pestañas a sus defaults)",
                  foreground="gray").pack(side="left", padx=8)

    def _param_row(self, fm, r, label, var, path):
        ttk.Label(fm, text=label).grid(row=r, column=0, sticky="w", pady=1)
        ttk.Entry(fm, textvariable=var, width=10).grid(row=r, column=1,
                                                       sticky="w", pady=1, padx=4)
        # descriptor: el comentario de esa clave en template.yaml (única fuente
        # de verdad — textos pobres se mejoran EN el template, no aquí)
        doc = self.tpl.get(path, ("", ""))[1]
        if doc:
            ttk.Label(fm, text=doc, foreground="gray").grid(
                row=r, column=2, sticky="w", padx=6)

    def _tab_productos(self, fm):
        r = 0
        for attr, label, path in self.prod_fields:
            self._param_row(fm, r, label, getattr(self, attr), path)
            r += 1
        self.radius_lbl = ttk.Label(fm, foreground="gray")
        self.radius_lbl.grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.v_res.trace_add("write", lambda *a: self._update_radius_lbl())
        self._update_radius_lbl()

    def _update_radius_lbl(self):
        # los radios de writers.gdal NO son campos: derivan de la resolución
        try:
            res = float(self.v_res.get())
            self.radius_lbl.config(text=(
                "radios writers.gdal calculados (no editables): DTM %s (res·√2, "
                "window_size 3) · DSM/density %s (res·√2/2, window_size 0)"
                % (round(res * math.sqrt(2), 4), round(res * math.sqrt(2) / 2, 4))))
        except ValueError:
            self.radius_lbl.config(text="radios calculados: — (resolución inválida)")

    def _tab_arboles(self, fm):
        ttk.Checkbutton(fm, text="habilitar detección de árboles (s07)",
                        variable=self.v_trees).grid(row=0, column=0, columnspan=3,
                                                    sticky="w", pady=(0, 4))
        r = 1
        for attr, label, path in self.tree_fields:
            self._param_row(fm, r, label, getattr(self, attr), path)
            r += 1
        self.det_radius_lbl = ttk.Label(fm, foreground="gray")
        self.det_radius_lbl.grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.v_t_det_res.trace_add("write", lambda *a: self._update_det_radius_lbl())
        self._update_det_radius_lbl()

    def _update_det_radius_lbl(self):
        try:
            d = float(self.v_t_det_res.get())
            self.det_radius_lbl.config(text="radio calculado (det_res·√2/2): %s"
                                            % round(d * math.sqrt(2) / 2, 4))
        except ValueError:
            self.det_radius_lbl.config(text="radio calculado: —")

    def _tab_avanzado(self, fm):
        tk.Label(fm, fg="white", bg="#a02020", justify="left", anchor="w", padx=6,
                 pady=4, text="Validados para bosque adulto denso. Cambiar altera la "
                              "clasificación de suelo: revisar hillshade del DTM antes "
                              "de aceptar resultados.").grid(
            row=0, column=0, columnspan=3, sticky="we", pady=(0, 6))
        r = 1
        for attr, label, path in self.adv_fields:
            self._param_row(fm, r, label, getattr(self, attr), path)
            r += 1

    def _load_profile_defaults(self, profile):
        """Defaults de todas las pestañas: template.yaml + overrides del perfil."""
        prof_res, smrf, _qc = ps.PROFILES[profile]
        for attr, _label, path in self.param_fields:
            getattr(self, attr).set(self.tpl.get(path, ("", ""))[0])
        self.v_res.set(ps.fmt_num(prof_res))
        self.v_vol_res.set(ps.fmt_num(prof_res))
        self.v_s_slope.set(str(smrf["slope"]))
        self.v_s_thr.set(str(smrf["threshold"]))
        self.v_trees.set(self.tpl.get(("trees", "enabled"), ("false", ""))[0] == "true")

    def _refresh_configs(self, select=None):
        self.configs = {os.path.basename(p): p for p in ps.list_configs()}
        self.cfg_combo["values"] = list(self.configs)
        if select and select in self.configs:
            self.cfg_var.set(select)
        elif self.configs and not self.cfg_var.get():
            self.cfg_combo.current(0)

    def _toggle_form(self):
        if self.form_visible:
            self.form.pack_forget()
        else:
            self.form.pack(fill="x")
        self.form_visible = not self.form_visible

    # ---- exploradores
    def _browse_root(self):
        d = filedialog.askdirectory(title="project_root (raíz de datos)")
        if d:
            self.v_root.set(d)
            if not self.v_dest.get():
                self.v_dest.set(os.path.join(d, "out"))
            laz = ps.find_laz_dir(d)
            if laz and not self.v_laz.get():
                self.v_laz.set(laz)
                self._update_laz_info()

    def _browse_dest(self):
        d = filedialog.askdirectory(title="Carpeta de destino (salidas del pipeline)",
                                    initialdir=self.v_root.get() or None)
        if d:
            self.v_dest.set(d)

    def _browse_laz(self):
        d = filedialog.askdirectory(title="Carpeta con *.laz",
                                    initialdir=self.v_root.get() or None)
        if d:
            self.v_laz.set(d)
            self._update_laz_info()

    def _update_laz_info(self):
        files, gb = ps.scan_laz(self.v_laz.get())
        self.laz_info.config(
            text="%d archivos, %.2f GB" % (len(files), gb) if files
            else "sin *.laz", foreground="black" if files else "red")

    def _browse_vector(self, var):
        p = filedialog.askopenfilename(title="Capa vectorial",
                                       filetypes=VECTOR_TYPES,
                                       initialdir=self.v_root.get() or None)
        if p:
            var.set(p)
        return p

    def _browse_aoi(self):
        if self._browse_vector(self.v_aoi):
            self.epsg_info.config(text="detectando EPSG...")
            threading.Thread(target=self._detect_epsg, daemon=True).start()

    def _detect_epsg(self):
        try:
            epsg, name = ps.detect_epsg(self.v_aoi.get())
        except Exception as e:  # noqa: BLE001
            epsg, name = None, str(e)
        def apply():
            if epsg:
                self.v_epsg.set(str(epsg))
                self.epsg_info.config(text="detectado: %s" % name)
            else:
                self.epsg_info.config(text="sin CRS declarado — ingresar a mano")
        self.root.after(0, apply)

    def _browse_ortho(self):
        p = filedialog.askopenfilename(
            title="Ortomosaico", initialdir=self.v_root.get() or None,
            filetypes=[("GeoTIFF", "*.tif *.tiff"), ("Todos", "*.*")])
        if p:
            self.v_ortho.set(p)

    def _on_profile(self):
        self._load_profile_defaults(self.v_perfil.get())

    def _load_config_into_form(self, event=None):
        """Modo edición: puebla las pestañas con los valores del config elegido
        (no con defaults). Guardar con el mismo nombre lo sobrescribe.
        """
        name = self.cfg_var.get()
        if not name:
            return
        cfg_path = self.configs[name]
        try:
            sys.path.insert(0, os.path.join(PIPE_DIR, "stages"))
            import common
            cfg = common.load_config(cfg_path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Config", "No se pudo leer %s: %s" % (name, e))
            return

        def opt(path):  # rutas opcionales: PENDIENTE del wizard -> campo vacío
            return "" if os.path.basename(path).startswith("PENDIENTE") else path

        p = cfg["paths"]
        self.v_name.set(str(cfg["project"]["name"]))
        self.v_root.set(cfg["_project_root"])
        self.v_dest.set(p["_out_dir"])
        self.v_laz.set(p["_input_laz_dir"])
        self._update_laz_info()
        self.v_aoi.set(p["_aoi_buffer"])
        self.v_predios.set(p["_predios"])
        self.v_uso.set(opt(p["_uso"]))
        self.v_acopio.set(opt(p["_stockpile_boundary"]))
        self.v_epsg.set(str(cfg["project"]["epsg"]))
        self.epsg_info.config(text="del config %s" % name)

        # perfil y ortho viven solo en los comentarios de cabecera del YAML
        self.v_ortho.set("")
        with open(cfg_path, encoding="utf-8") as fh:
            for line in fh:
                if not line.startswith("#"):
                    break
                m = re.search(r"perfil: (\w+)", line)
                if m and m.group(1) in ps.PROFILES:
                    self.v_perfil.set(m.group(1))
                m = re.match(r"^# ortho: (\S+)", line)
                if m:
                    self.v_ortho.set(m.group(1).strip('"'))

        def dig(d, path):
            for k in path:
                d = d[k]
            return d

        for attr, _label, path in self.param_fields:
            try:
                getattr(self, attr).set(str(dig(cfg, path)))
            except (KeyError, TypeError):
                pass  # clave ausente en configs viejos: queda el valor actual
        try:
            self.v_trees.set(bool(dig(cfg, ("trees", "enabled"))))
        except (KeyError, TypeError):
            pass
        if not self.form_visible:
            self._toggle_form()

    # ---- guardar: validar + bounds + escribir YAML (todo vía project_setup)
    def _save_config(self):
        try:
            name = self.v_name.get().strip()
            if not name or " " in name:
                raise ValueError("Nombre requerido, sin espacios")
            root = self.v_root.get().strip()
            if not os.path.isdir(root):
                raise ValueError("project_root no existe: %s" % root)
            laz_dir = self.v_laz.get().strip()
            files, _gb = ps.scan_laz(laz_dir) if os.path.isdir(laz_dir) else ([], 0)
            if not files:
                raise ValueError("no hay *.laz en la carpeta LAZ")
            aoi = ps.validate_vector(self.v_aoi.get().strip())
            predios = ps.validate_vector(self.v_predios.get().strip())
            uso = (ps.validate_vector(self.v_uso.get().strip())
                   if self.v_uso.get().strip() else None)
            acopio = (ps.validate_vector(self.v_acopio.get().strip())
                      if self.v_acopio.get().strip() else None)
            ortho = self.v_ortho.get().strip() or None
            epsg = ps.validate_epsg(self.v_epsg.get().strip())
            nums = {}
            for attr, label, path in self.param_fields:
                raw = getattr(self, attr).get().strip()
                try:
                    nums[path] = float(raw)
                except ValueError:
                    raise ValueError("%s: número inválido (%r)" % (label, raw))
            res = nums[("grid", "resolution")]
            if res <= 0:
                raise ValueError("resolución debe ser > 0")
            dest = self.v_dest.get().strip() or None
            default_dest = os.path.normpath(os.path.join(root, "out"))
            if dest and os.path.normpath(dest) == default_dest:
                dest = None  # default del template: {project_root}/out
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Validación", str(e))
            return

        cfg_path = ps.config_path(name)
        if os.path.exists(cfg_path) and not messagebox.askyesno(
                "Sobrescribir", "Ya existe %s. ¿Sobrescribir?"
                % os.path.basename(cfg_path)):
            return
        bounds = ps.compute_bounds(aoi, epsg, res)
        if not messagebox.askyesno(
                "Confirmar bounds",
                "grid.bounds calculado del AOI (redondeado a %s m):\n\n%s\n\n"
                "¿Usar estos bounds y guardar el config?\n"
                "(editables a mano después en el YAML)"
                % (ps.fmt_num(res), ps.format_bounds(bounds))):
            return
        # pestañas -> params del YAML (strings tal cual; radios derivados aquí)
        params = {path: getattr(self, attr).get().strip()
                  for attr, _label, path in self.param_fields}
        params[("trees", "enabled")] = "true" if self.v_trees.get() else "false"
        params[("trees", "det_radius")] = str(round(
            nums[("trees", "det_res")] * math.sqrt(2) / 2, 4))
        ps.write_config(cfg_path, name, epsg, root, laz_dir, aoi, predios,
                        uso, acopio, ortho, self.v_perfil.get(), res, bounds,
                        output_dir=dest, params=params)
        pend = "" if uso and acopio else \
            "\n\nOJO: hay rutas PENDIENTES (uso/acopio); completar en el YAML antes de correr."
        messagebox.showinfo("Config guardado", "Escrito: %s%s" % (cfg_path, pend))
        self._refresh_configs(select=os.path.basename(cfg_path))
        self._toggle_form()

    # ------------------------------------------------------------- zona B
    def _zone_b(self, parent):
        f = ttk.LabelFrame(parent, text="B) EJECUCIÓN", padding=6)
        f.pack(fill="x", pady=6)
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.btn_dry = ttk.Button(row, text="Dry-run",
                                  command=lambda: self._launch(dry=True))
        self.btn_dry.pack(side="left")
        self.btn_run = ttk.Button(row, text="EJECUTAR",
                                  command=lambda: self._launch(dry=False))
        self.btn_run.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(row, text="Cancelar", state="disabled",
                                     command=self._cancel)
        self.btn_cancel.pack(side="left")
        self.status = tk.Label(row, text="listo", width=28, anchor="w")
        self.status.pack(side="left", padx=12)
        self.progress = ttk.Progressbar(f, maximum=N_STAGES * 2)
        self.progress.pack(fill="x", pady=(6, 0))
        self.stage_lbl = ttk.Label(f, text="")
        self.stage_lbl.pack(anchor="w")

    def _selected_config(self):
        name = self.cfg_var.get()
        if not name:
            messagebox.showwarning("Config", "Selecciona o crea un config primero.")
            return None
        return self.configs[name]

    def _launch(self, dry):
        cfg = self._selected_config()
        if not cfg or self.proc:
            return
        cmd = [sys.executable, "-u", RUN_PIPELINE, "--config", cfg, "--all"]
        if dry:
            cmd.append("--dry-run")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self._clear_log()
        self._append("$ %s\n\n" % " ".join(cmd))
        self.proc = subprocess.Popen(cmd, cwd=PIPE_DIR, env=env,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8",
                                     errors="replace", bufsize=1)
        self.cancelled = False
        self.progress["value"] = 0
        self.stage_lbl.config(text="")
        self._set_status("ejecutando..." if not dry else "dry-run...", "black")
        self.btn_run.config(state="disabled")
        self.btn_dry.config(state="disabled")
        self.btn_cancel.config(state="normal")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout:
            self.q.put(("line", line))
        proc.wait()
        self.q.put(("done", proc.returncode))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self._parse_line(payload)
                    self._append(payload)
                else:
                    self._finished(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _parse_line(self, line):
        m = RE_INICIO.match(line)
        if m:
            n = int(m.group(1)[1:])
            self.progress["value"] = (n - 1) * 2 + 1
            self.stage_lbl.config(text="Etapa actual: %s (%s)"
                                  % (m.group(1), m.group(2)))
            return
        m = RE_FIN.match(line)
        if m:
            self.progress["value"] = int(m.group(1)[1:]) * 2
            self.stage_lbl.config(text="Terminada: %s (%s, %ss)"
                                  % (m.group(1), m.group(2), m.group(3)))
            return
        m = RE_LOG.match(line)
        if m:
            self.last_log = m.group(1).strip()
            return
        m = RE_OUTDIR.match(line)
        if m:
            self.last_outdir = m.group(1).strip()

    def _finished(self, code):
        self.proc = None
        self.btn_run.config(state="normal")
        self.btn_dry.config(state="normal")
        self.btn_cancel.config(state="disabled")
        if self.cancelled:
            self._set_status("cancelado por el usuario", "gray25")
        elif code == 0:
            self.progress["value"] = self.progress["maximum"]
            self._set_status("OK (exit 0)", "dark green")
        elif code == 2:
            self._set_status("QC STOP (exit 2) — revisar log", "dark orange")
        elif code == 3:
            self._set_status("CONFIG ERROR (exit 3)", "red")
        else:
            self._set_status("ERROR (exit %s) — abrir el log" % code, "red")

    def _set_status(self, text, color):
        self.status.config(text=text, fg=color)

    def _cancel(self):
        if self.proc and messagebox.askyesno(
                "Cancelar", "¿Terminar el proceso en curso?"):
            self.cancelled = True
            if self.proc:
                if os.name == "nt":
                    # taskkill /T mata también los hijos (pdal); terminate() no
                    subprocess.run(["taskkill", "/PID", str(self.proc.pid),
                                    "/T", "/F"], capture_output=True)
                else:
                    self.proc.terminate()

    # ------------------------------------------------------------- zona C
    def _zone_c(self, parent):
        f = ttk.LabelFrame(parent, text="C) LOG", padding=6)
        f.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(f, height=18, state="disabled",
                                             font=("Consolas", 9), wrap="none")
        self.log.pack(fill="both", expand=True)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Abrir carpeta de salida",
                   command=self._open_outdir).pack(side="left")
        ttk.Button(row, text="Abrir log",
                   command=self._open_log).pack(side="left", padx=6)

    def _append(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    @staticmethod
    def _open_path(path):
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _open_outdir(self):
        if self.last_outdir and os.path.isdir(self.last_outdir):
            self._open_path(self.last_outdir)
        else:
            messagebox.showinfo("Salida", "Aún no hay corrida en esta sesión "
                                          "(el out_dir se toma de la salida del proceso).")

    def _open_log(self):
        if self.last_log and os.path.exists(self.last_log):
            self._open_path(self.last_log)
        else:
            messagebox.showinfo("Log", "Aún no hay log de corrida en esta sesión.")

    # ------------------------------------------------------------- cierre
    def on_close(self):
        if self.proc:
            if not messagebox.askyesno(
                    "Salir", "Hay un proceso corriendo. ¿Cancelarlo y salir?"):
                return
            self.cancelled = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.proc.pid),
                                "/T", "/F"], capture_output=True)
            else:
                self.proc.terminate()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
