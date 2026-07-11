"""GUI de escritorio del pipeline LiDAR — solo tkinter/ttk (stdlib).

    python gui/pipeline_gui.py

Tres zonas: A) proyecto (elegir config existente o crear uno nuevo con
scripts/project_setup.py), B) ejecución (run_pipeline.py como subprocess,
salida en vivo vía hilo+queue, progreso por banners de etapa), C) log.

REGLA: la GUI no contiene lógica de pipeline ni parámetros — solo escribe/
selecciona configs y lanza el proceso. Los valores viven en el YAML.
"""
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
        self._refresh_configs()

        # ---- formulario de proyecto nuevo (oculto hasta pulsar el botón)
        self.form = ttk.Frame(f, padding=(4, 8, 4, 2))
        self.form_visible = False
        fm = self.form
        self.v_name = tk.StringVar()
        self.v_root = tk.StringVar()
        self.v_dest = tk.StringVar()
        self.v_laz = tk.StringVar()
        self.v_aoi = tk.StringVar()
        self.v_predios = tk.StringVar()
        self.v_uso = tk.StringVar()
        self.v_acopio = tk.StringVar()
        self.v_ortho = tk.StringVar()
        self.v_epsg = tk.StringVar()
        self.v_perfil = tk.StringVar(value="forestal")
        self.v_res = tk.StringVar(value=ps.fmt_num(ps.PROFILES["forestal"][0]))

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
        ttk.Label(pf, text="(solo 'forestal' validado)",
                  foreground="gray").pack(side="left", padx=8)
        entry_row(11, "Resolución (m/píxel)", self.v_res)
        ttk.Button(fm, text="Guardar config",
                   command=self._save_config).grid(row=12, column=1, sticky="w", pady=6)

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
        self.v_res.set(ps.fmt_num(ps.PROFILES[self.v_perfil.get()][0]))

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
            res = float(self.v_res.get())
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
        ps.write_config(cfg_path, name, epsg, root, laz_dir, aoi, predios,
                        uso, acopio, ortho, self.v_perfil.get(), res, bounds,
                        output_dir=dest)
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

    def _open_outdir(self):
        if self.last_outdir and os.path.isdir(self.last_outdir):
            os.startfile(self.last_outdir)
        else:
            messagebox.showinfo("Salida", "Aún no hay corrida en esta sesión "
                                          "(el out_dir se toma de la salida del proceso).")

    def _open_log(self):
        if self.last_log and os.path.exists(self.last_log):
            os.startfile(self.last_log)
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
