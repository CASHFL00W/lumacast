"""Hauptfenster von LumaCast: Profile verwalten, Einstellungen, Start/Stop."""
from __future__ import annotations

import copy
import multiprocessing as mp
import queue
import sys
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import audio, capture, config, lossless
from .config import APP_NAME, APP_VERSION, DEFAULT_AUDIO, TEST_PATTERN

ACCENT = "#f5a524"
ACCENT_HOVER = "#d98e14"
GREEN = "#1f9d55"
GREEN_HOVER = "#17804a"
RED = "#c0392b"
RED_HOVER = "#a33024"
MUTED = "#9aa3b2"
CARD = ("#eceff4", "#1c2029")

RESOLUTIONS = ["1280x720", "1920x1080", "2560x1440", "3840x2160"]
FPS_CHOICES = ["30", "60", "120"]


def list_monitors() -> list[str]:
    try:
        import glfw
        if not glfw.init():
            return []
        names = []
        for i, mon in enumerate(glfw.get_monitors() or []):
            mode = glfw.get_video_mode(mon)
            name = glfw.get_monitor_name(mon)
            if isinstance(name, bytes):
                name = name.decode(errors="ignore")
            names.append(f"{i + 1}: {name} ({mode.size.width}x{mode.size.height} @ {mode.refresh_rate} Hz)")
        glfw.terminate()
        return names
    except Exception:
        return []


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1100x760")
        self.minsize(980, 680)
        if sys.platform == "win32":
            try:
                self.iconbitmap(config.resource_path("icon.ico"))
            except Exception:
                pass

        self.store = config.load_store()
        self.profile = self._active_profile()
        self._loading = False
        self._save_job = None
        self._live_job = None
        self.refreshers: list = []

        self.proc = None
        self.cmd_q = None
        self.status_q = None
        self.running_profile = None
        self._had_error = False
        self.mp = mp.get_context("spawn")

        self.load_devices()
        self.build()
        self.load_profile_into_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.poll_status)

    # ============================================================== Daten
    def _active_profile(self) -> dict:
        for p in self.store["profiles"]:
            if p["id"] == self.store["active"]:
                return p
        return self.store["profiles"][0]

    def load_devices(self):
        self.video_devices = [TEST_PATTERN] + capture.list_video_devices()
        self.audio_inputs = audio.list_devices("input")
        self.audio_outputs = [DEFAULT_AUDIO] + audio.list_devices("output")
        self.monitors = list_monitors() or ["1: Hauptmonitor"]

    def schedule_save(self):
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(400, self._save_now)

    def _save_now(self):
        self._save_job = None
        try:
            config.save_store(self.store)
        except OSError as exc:
            self.set_status(f"Speichern fehlgeschlagen: {exc}", error=True)

    # ============================================================== Aufbau
    def build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ---------------- Seitenleiste
        side = ctk.CTkFrame(self, width=240, corner_radius=0)
        side.grid(row=0, column=0, rowspan=2, sticky="nsw")
        side.grid_propagate(False)
        side.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(side, text=APP_NAME, font=ctk.CTkFont(size=28, weight="bold"),
                     text_color=ACCENT).grid(row=0, column=0, padx=20, pady=(22, 0), sticky="w")
        ctk.CTkLabel(side, text="Konsole · Capture · Upscaling", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).grid(row=1, column=0, padx=20, pady=(0, 16), sticky="w")
        ctk.CTkLabel(side, text="PROFILE", text_color=MUTED,
                     font=ctk.CTkFont(size=11, weight="bold")).grid(row=2, column=0, padx=20, sticky="w")
        self.profile_list = ctk.CTkScrollableFrame(side, fg_color="transparent")
        self.profile_list.grid(row=3, column=0, padx=10, pady=6, sticky="nsew")
        btns = ctk.CTkFrame(side, fg_color="transparent")
        btns.grid(row=4, column=0, padx=14, pady=(4, 18), sticky="ew")
        for i, (txt, cmd) in enumerate((("Neu", self.new_profile), ("Kopie", self.copy_profile),
                                        ("Löschen", self.delete_profile))):
            ctk.CTkButton(btns, text=txt, width=66, height=30, command=cmd,
                          fg_color="#2b303b", hover_color="#3a404d").grid(row=0, column=i, padx=3)

        # ---------------- Hauptbereich
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=18, pady=(16, 6))
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(main, fg_color=CARD, corner_radius=12)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text="Name").grid(row=0, column=0, padx=(16, 8), pady=(12, 4), sticky="w")
        self.name_var = tk.StringVar()
        ctk.CTkEntry(head, textvariable=self.name_var).grid(row=0, column=1, padx=(0, 16), pady=(12, 4), sticky="ew")
        ctk.CTkLabel(head, text="Beschreibung").grid(row=1, column=0, padx=(16, 8), pady=(4, 12), sticky="w")
        self.desc_var = tk.StringVar()
        ctk.CTkEntry(head, textvariable=self.desc_var,
                     placeholder_text="z. B. PS5 · Shooter · 1080p60").grid(row=1, column=1, padx=(0, 16),
                                                                         pady=(4, 12), sticky="ew")
        self.name_var.trace_add("write", lambda *_: self._on_meta())
        self.desc_var.trace_add("write", lambda *_: self._on_meta())

        tabs = ctk.CTkTabview(main, segmented_button_selected_color=ACCENT,
                              segmented_button_selected_hover_color=ACCENT_HOVER)
        tabs.grid(row=1, column=0, sticky="nsew")
        self.tabs = tabs
        for name in ("Video", "Bild", "Skalierung", "Audio", "Ausgabe", "Frame Gen"):
            tabs.add(name)
        self.build_video(tabs.tab("Video"))
        self.build_image(tabs.tab("Bild"))
        self.build_scaling(tabs.tab("Skalierung"))
        self.build_audio(tabs.tab("Audio"))
        self.build_output(tabs.tab("Ausgabe"))
        self.build_framegen(tabs.tab("Frame Gen"))

        # ---------------- Fußleiste
        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        bar.grid(row=1, column=1, sticky="ew", padx=18, pady=(4, 16))
        bar.grid_columnconfigure(1, weight=1)
        self.start_btn = ctk.CTkButton(bar, text="▶  Start", width=190, height=46,
                                       font=ctk.CTkFont(size=16, weight="bold"),
                                       fg_color=GREEN, hover_color=GREEN_HOVER, command=self.toggle_run)
        self.start_btn.grid(row=0, column=0, rowspan=2, padx=14, pady=12)
        self.status_label = ctk.CTkLabel(bar, text="Bereit.", anchor="w", font=ctk.CTkFont(size=14))
        self.status_label.grid(row=0, column=1, sticky="ew", padx=6, pady=(12, 0))
        self.stats_label = ctk.CTkLabel(bar, text="", anchor="w", text_color=MUTED, font=ctk.CTkFont(size=12))
        self.stats_label.grid(row=1, column=1, sticky="ew", padx=6, pady=(0, 12))

    # ------------------------------------------------------------ Bausteine
    def _section(self, parent, title: str, row: int, hint: str = ""):
        frame = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        frame.grid(row=row, column=0, sticky="ew", padx=6, pady=6)
        frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, columnspan=3, padx=16, pady=(12, 2 if hint else 8), sticky="w")
        if hint:
            ctk.CTkLabel(frame, text=hint, text_color=MUTED, justify="left", wraplength=720,
                         font=ctk.CTkFont(size=12)).grid(row=1, column=0, columnspan=3, padx=16,
                                                         pady=(0, 8), sticky="w")
        frame._next_row = 2
        return frame

    def _row(self, frame, label: str):
        r = frame._next_row
        frame._next_row += 1
        ctk.CTkLabel(frame, text=label, width=170, anchor="w").grid(row=r, column=0, padx=(16, 8), pady=5, sticky="w")
        return r

    def _get(self, section, key):
        return self.profile[section][key]

    def _set(self, section, key, value):
        if self._loading:
            return
        self.profile[section][key] = value
        self.on_changed()

    def option(self, frame, label, section, key, choices, getter_choices=None, width=320):
        """choices: Liste von (Wert, Anzeige)."""
        r = self._row(frame, label)

        def labels():
            ch = getter_choices() if getter_choices else choices
            return ch, [c[1] for c in ch]

        ch, labs = labels()
        menu = ctk.CTkOptionMenu(frame, values=labs or ["—"], width=width, dynamic_resizing=False,
                                 fg_color="#2b303b", button_color="#3a404d", button_hover_color="#4a5160")
        menu.grid(row=r, column=1, padx=(0, 16), pady=5, sticky="w")

        def on_pick(lab):
            cur, _ = labels()
            for value, text in cur:
                if text == lab:
                    self._set(section, key, value)
                    return

        menu.configure(command=on_pick)

        def refresh():
            cur, labs2 = labels()
            menu.configure(values=labs2 or ["—"])
            value = self._get(section, key)
            text = next((t for v, t in cur if v == value), None)
            if text is None:
                text = str(value) if value not in ("", None) else (labs2[0] if labs2 else "—")
            menu.set(text)

        self.refreshers.append(refresh)
        return menu

    def segmented(self, frame, label, section, key, choices):
        r = self._row(frame, label)
        seg = ctk.CTkSegmentedButton(frame, values=[c[1] for c in choices], selected_color=ACCENT,
                                     selected_hover_color=ACCENT_HOVER)
        seg.grid(row=r, column=1, padx=(0, 16), pady=5, sticky="w")

        def on_pick(lab):
            for value, text in choices:
                if text == lab:
                    self._set(section, key, value)

        seg.configure(command=on_pick)

        def refresh():
            value = self._get(section, key)
            seg.set(next((t for v, t in choices if v == value), choices[0][1]))

        self.refreshers.append(refresh)
        return seg

    def slider(self, frame, label, section, key, lo, hi, steps=None, fmt="{:+d}", cast=int):
        r = self._row(frame, label)
        value_lbl = ctk.CTkLabel(frame, text="", width=56, anchor="e")
        value_lbl.grid(row=r, column=2, padx=(0, 16))
        sl = ctk.CTkSlider(frame, from_=lo, to=hi, number_of_steps=steps or int(hi - lo),
                           button_color=ACCENT, button_hover_color=ACCENT_HOVER, progress_color=ACCENT)
        sl.grid(row=r, column=1, padx=(0, 8), pady=5, sticky="ew")

        def on_move(v):
            v = cast(round(v, 2)) if cast is float else cast(round(v))
            value_lbl.configure(text=fmt.format(v))
            self._set(section, key, v)

        sl.configure(command=on_move)

        def refresh():
            v = self._get(section, key)
            sl.set(v)
            value_lbl.configure(text=fmt.format(v))

        self.refreshers.append(refresh)
        return sl

    def switch(self, frame, label, section, key, text=""):
        r = self._row(frame, label)
        sw = ctk.CTkSwitch(frame, text=text, progress_color=ACCENT,
                           command=lambda: self._set(section, key, bool(sw.get())))
        sw.grid(row=r, column=1, padx=(0, 16), pady=5, sticky="w")

        def refresh():
            sw.select() if self._get(section, key) else sw.deselect()

        self.refreshers.append(refresh)
        return sw

    def entry(self, frame, label, section, key, width=320):
        r = self._row(frame, label)
        var = tk.StringVar()
        ent = ctk.CTkEntry(frame, textvariable=var, width=width)
        ent.grid(row=r, column=1, padx=(0, 16), pady=5, sticky="w")
        var.trace_add("write", lambda *_: self._set(section, key, var.get()))

        def refresh():
            var.set(self._get(section, key))

        self.refreshers.append(refresh)
        return ent, r

    # ------------------------------------------------------------- Tabs
    def build_video(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        sec = self._section(tab, "Capture-Karte", 0,
                            "Wähle deine Capture-Karte. Für 30-FPS-Spiele trotzdem 60 FPS wählen – "
                            "Konsolen geben fast immer 60 Hz aus. Die Karte muss an USB 3 hängen und "
                            "darf nicht gleichzeitig in OBS o. Ä. offen sein.")
        self.option(sec, "Gerät", "video", "device", None,
                    getter_choices=lambda: [(d, d) for d in self.video_devices])
        ctk.CTkButton(sec, text="Geräte neu suchen", width=150, fg_color="#2b303b", hover_color="#3a404d",
                      command=self.refresh_devices).grid(row=sec._next_row - 1, column=2, padx=(0, 16))
        res_choices = [(r, r) for r in RESOLUTIONS]
        r = self._row(sec, "Auflösung")
        self.res_menu = ctk.CTkOptionMenu(sec, values=RESOLUTIONS, width=320, dynamic_resizing=False,
                                          fg_color="#2b303b", button_color="#3a404d", button_hover_color="#4a5160",
                                          command=self._on_res)
        self.res_menu.grid(row=r, column=1, padx=(0, 16), pady=5, sticky="w")
        self.refreshers.append(lambda: self.res_menu.set(f"{self.profile['video']['width']}x{self.profile['video']['height']}"))
        del res_choices
        self.option(sec, "Bildrate", "video", "fps", [(int(f), f"{f} FPS") for f in FPS_CHOICES])
        self.option(sec, "Format", "video", "format",
                    [("Auto", "Automatisch (empfohlen)"), ("YUY2", "YUY2 (unkomprimiert)"),
                     ("NV12", "NV12 (unkomprimiert)"), ("MJPG", "MJPEG (komprimiert)")])

    def _on_res(self, text):
        w, h = (int(x) for x in text.split("x"))
        if self._loading:
            return
        self.profile["video"]["width"] = w
        self.profile["video"]["height"] = h
        self.on_changed()

    def build_image(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        sec = self._section(scroll, "Bildmodus", 0,
                            "Original = unverändert · Manuell = deine Regler · Auto = analysiert das Bild und "
                            "passt Schwarz-/Weißpunkt und Helligkeit laufend an · Hybrid = Auto + deine Regler. "
                            "Änderungen siehst du sofort im laufenden Bild.")
        self.segmented(sec, "Modus", "image", "mode",
                       [("original", "Original"), ("manual", "Manuell"), ("auto", "Auto"), ("hybrid", "Hybrid")])
        self.option(sec, "HDMI-Farbbereich", "image", "input_range",
                    [("full", "Voll (0–255)"), ("limited", "Begrenzt (16–235)")])
        self.slider(sec, "Auto-Stärke", "image", "auto_strength", 0, 100, fmt="{:d}")

        sec2 = self._section(scroll, "Manuelle Regler", 1, "Wirken in den Modi Manuell und Hybrid.")
        for key, label in (("brightness", "Helligkeit"), ("contrast", "Kontrast"), ("saturation", "Sättigung"),
                           ("vibrance", "Dynamik"), ("gamma", "Mitteltöne (Gamma)"),
                           ("temperature", "Farbtemperatur"), ("black_level", "Schwarzwert")):
            self.slider(sec2, label, "image", key, -100, 100)
        ctk.CTkButton(sec2, text="Regler zurücksetzen", width=170, fg_color="#2b303b", hover_color="#3a404d",
                      command=self.reset_image).grid(row=sec2._next_row, column=1, padx=(0, 16), pady=(6, 14),
                                                     sticky="w")

    def reset_image(self):
        for key in ("brightness", "contrast", "saturation", "vibrance", "gamma", "temperature", "black_level"):
            self.profile["image"][key] = 0
        self.load_profile_into_ui()
        self.on_changed()

    def build_scaling(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        sec = self._section(tab, "Hochskalieren", 0,
                            "Läuft komplett auf deiner Grafikkarte. Lanczos ist am schärfsten, Bikubisch etwas "
                            "weicher, Pixelgenau für Retro-Konsolen.")
        self.segmented(sec, "Verfahren", "upscale", "method",
                       [("nearest", "Pixelgenau"), ("bilinear", "Bilinear"), ("bicubic", "Bikubisch"),
                        ("lanczos", "Lanczos")])
        self.switch(sec, "Halo-Schutz", "upscale", "anti_ringing", "verhindert helle Ränder an Kanten")
        sec2 = self._section(tab, "Schärfen", 1,
                             "Kontrastadaptives Schärfen (nach AMD CAS): schärft feine Details stärker als "
                             "harte Kanten, damit nichts überzeichnet wirkt. 0 = aus.")
        self.slider(sec2, "Schärfe", "upscale", "sharpness", 0, 100, fmt="{:d}")
        sec3 = self._section(tab, "Bildgröße", 2)
        self.segmented(sec3, "Darstellung", "output", "scale_mode",
                       [("fit", "Einpassen"), ("fill", "Füllen"), ("stretch", "Strecken"),
                        ("integer", "Ganzzahlig")])

    def build_audio(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        sec = self._section(tab, "Ton durchleiten", 0,
                            "Der Ton der Capture-Karte wird mit geringer Verzögerung an dein Ausgabegerät "
                            "weitergegeben. Das Eingangsgerät heißt meist wie deine Capture-Karte.")
        self.switch(sec, "Aktiv", "audio", "enabled")
        self.option(sec, "Eingang (Capture)", "audio", "input", None,
                    getter_choices=lambda: [("", "— bitte wählen —")] + [(d, d) for d in self.audio_inputs],
                    width=420)
        self.option(sec, "Ausgabe", "audio", "output", None,
                    getter_choices=lambda: [(d, "Windows-Standard" if d == DEFAULT_AUDIO else d)
                                            for d in self.audio_outputs], width=420)
        self.slider(sec, "Lautstärke", "audio", "volume", 0, 150, fmt="{:d} %")

    def build_output(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        sec = self._section(tab, "Bildschirm", 0,
                            "Die Ausgabe öffnet sich als randloses Vollbildfenster auf dem gewählten Monitor.")
        self.option(sec, "Monitor", "output", "monitor", None,
                    getter_choices=lambda: list(enumerate(self.monitors)), width=420)
        self.switch(sec, "VSync", "output", "vsync", "aus = geringste Latenz (ideal mit G-Sync/FreeSync)")
        self.switch(sec, "Info-Overlay", "output", "overlay", "FPS, Latenz und Auflösung einblenden")
        self.switch(sec, "Esc beendet", "output", "esc_closes")
        sec2 = self._section(tab, "Tasten im Ausgabefenster", 1)
        keys = ("F1  Info-Overlay ein/aus\nF2  Vergleich: links Original, rechts bearbeitet\n"
                "F3  Skalierung durchschalten\nF4  Bildbearbeitung kurz aus (Original)\n"
                "F5  Schärfe in Stufen\nF11 Fenster / Vollbild\nEsc Ausgabe beenden")
        ctk.CTkLabel(sec2, text=keys, justify="left", font=ctk.CTkFont(family="Consolas", size=13)).grid(
            row=2, column=0, columnspan=3, padx=16, pady=(0, 14), sticky="w")

    def build_framegen(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        sec = self._section(tab, "Frame Generation über Lossless Scaling", 0,
                            "Mehr FPS durch berechnete Zwischenbilder (z. B. 60 → 120/180/240). Dafür nutzt "
                            "LumaCast das Programm „Lossless Scaling“ (separat auf Steam zu kaufen). LumaCast "
                            "startet es mit, öffnet die Ausgabe als randloses Fenster und drückt nach kurzer "
                            "Wartezeit den Skalier-Hotkey. In Lossless Scaling Frame Generation auf LSFG "
                            "stellen und den Scaling-Typ auf „Off“, da LumaCast bereits hochskaliert.")
        self.switch(sec, "Aktiv", "framegen", "lossless", "Lossless Scaling mit dem Profil starten")
        ent, r = self.entry(sec, "Pfad", "framegen", "ls_path", width=420)
        ctk.CTkButton(sec, text="…", width=40, fg_color="#2b303b", hover_color="#3a404d",
                      command=self.browse_ls).grid(row=r, column=2, padx=(0, 16))
        self.ls_status = ctk.CTkLabel(sec, text="", text_color=MUTED, anchor="w")
        self.ls_status.grid(row=sec._next_row, column=1, columnspan=2, padx=(0, 16), sticky="w")
        sec._next_row += 1
        self.switch(sec, "Hotkey senden", "framegen", "send_hotkey", "Skalieren automatisch auslösen")
        self.entry(sec, "Hotkey", "framegen", "hotkey", width=180)
        self.slider(sec, "Wartezeit", "framegen", "delay", 0.5, 10.0, steps=19, fmt="{:.1f} s", cast=float)
        self.refreshers.append(self.update_ls_status)

    def browse_ls(self):
        path = filedialog.askopenfilename(title="LosslessScaling.exe auswählen",
                                          filetypes=[("Programm", "*.exe"), ("Alle Dateien", "*.*")])
        if path:
            self.profile["framegen"]["ls_path"] = path
            self.load_profile_into_ui()
            self.on_changed()

    def update_ls_status(self):
        found = lossless.find_lossless(self.profile["framegen"].get("ls_path", ""))
        if found:
            self.ls_status.configure(text=f"✓ Gefunden: {found}", text_color="#5cc98a")
        else:
            self.ls_status.configure(text="Nicht gefunden – Pfad zu LosslessScaling.exe angeben.",
                                     text_color=MUTED)

    # ============================================================ Profile
    def rebuild_profile_list(self):
        for child in self.profile_list.winfo_children():
            child.destroy()
        for p in self.store["profiles"]:
            active = p["id"] == self.profile["id"]
            text = p["name"] or "(ohne Namen)"
            if p.get("description"):
                text += f"\n{p['description'][:30]}"
            btn = ctk.CTkButton(self.profile_list, text=text, anchor="w", height=44,
                                fg_color=ACCENT if active else "transparent",
                                text_color="#111111" if active else ("#1a1a1a", "#e6e8ee"),
                                hover_color=ACCENT_HOVER if active else "#2b303b",
                                command=lambda pid=p["id"]: self.select_profile(pid))
            btn.pack(fill="x", pady=3)

    def select_profile(self, pid: str):
        if pid == self.profile["id"]:
            return
        self.store["active"] = pid
        self.profile = self._active_profile()
        self.load_profile_into_ui()
        self.schedule_save()
        if self.is_running():
            self.restart()

    def new_profile(self):
        p = config.new_profile(f"Profil {len(self.store['profiles']) + 1}")
        self.store["profiles"].append(p)
        self.select_profile(p["id"])

    def copy_profile(self):
        base = copy.deepcopy(self.profile)
        base.pop("id", None)
        p = config.new_profile(f"{self.profile['name']} (Kopie)", base)
        self.store["profiles"].append(p)
        self.select_profile(p["id"])

    def delete_profile(self):
        if len(self.store["profiles"]) <= 1:
            messagebox.showinfo(APP_NAME, "Mindestens ein Profil muss bleiben.")
            return
        if not messagebox.askyesno(APP_NAME, f"Profil „{self.profile['name']}“ löschen?"):
            return
        self.store["profiles"] = [p for p in self.store["profiles"] if p["id"] != self.profile["id"]]
        self.store["active"] = self.store["profiles"][0]["id"]
        self.profile = self._active_profile()
        self.load_profile_into_ui()
        self.schedule_save()

    def _fill_defaults(self):
        v = self.profile["video"]
        if not v.get("device"):
            real = [d for d in self.video_devices if d != TEST_PATTERN]
            if real:
                v["device"] = real[0]
        a = self.profile["audio"]
        if not a.get("input") and v.get("device") and v["device"] != TEST_PATTERN:
            a["input"] = audio.guess_capture_audio(v["device"], self.audio_inputs)

    def load_profile_into_ui(self):
        self._fill_defaults()
        self._loading = True
        try:
            self.name_var.set(self.profile.get("name", ""))
            self.desc_var.set(self.profile.get("description", ""))
            for refresh in self.refreshers:
                refresh()
        finally:
            self._loading = False
        self.rebuild_profile_list()

    def _on_meta(self):
        if self._loading:
            return
        self.profile["name"] = self.name_var.get()
        self.profile["description"] = self.desc_var.get()
        self.schedule_save()
        if self._live_job is None:
            self._live_job = self.after(500, self._refresh_list_later)

    def _refresh_list_later(self):
        self._live_job = None
        self.rebuild_profile_list()

    def refresh_devices(self):
        self.load_devices()
        self.load_profile_into_ui()
        self.set_status(f"{len(self.video_devices) - 1} Capture-Gerät(e), {len(self.audio_inputs)} Audio-Eingänge, "
                        f"{len(self.monitors)} Monitor(e) gefunden.")

    # ============================================================ Änderungen
    def on_changed(self):
        self.schedule_save()
        if self.is_running():
            if config.needs_restart(self.running_profile, self.profile):
                self.start_btn.configure(text="↻  Neu starten", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                         text_color="#111111")
                self.set_status("Diese Änderung wird nach einem Neustart der Ausgabe aktiv.")
            self._send_live()

    def _send_live(self):
        try:
            self.cmd_q.put_nowait({"type": "update", "profile": copy.deepcopy(self.profile)})
        except Exception:
            pass

    # ============================================================ Ausgabe
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def toggle_run(self):
        if self.is_running():
            if self.running_profile is not None and config.needs_restart(self.running_profile, self.profile):
                self.restart()
            else:
                self.stop()
        else:
            self.start()

    def _prepare_profile(self):
        v = self.profile["video"]
        if not v.get("device"):
            real = [d for d in self.video_devices if d != TEST_PATTERN]
            v["device"] = real[0] if real else TEST_PATTERN
        a = self.profile["audio"]
        if a.get("enabled") and not a.get("input") and v["device"] != TEST_PATTERN:
            a["input"] = audio.guess_capture_audio(v["device"], self.audio_inputs)
        self.load_profile_into_ui()
        self.schedule_save()

    def start(self):
        from .renderer import render_main
        self._prepare_profile()
        self.cmd_q = self.mp.Queue()
        self.status_q = self.mp.Queue()
        self.running_profile = copy.deepcopy(self.profile)
        self._had_error = False
        self.proc = self.mp.Process(target=render_main, args=(self.running_profile, self.cmd_q, self.status_q),
                                    daemon=True)
        self.proc.start()
        self.start_btn.configure(text="■  Stopp", fg_color=RED, hover_color=RED_HOVER, text_color="#ffffff")
        self.set_status(f"Starte „{self.profile['name']}“ …")

    def stop(self, wait: bool = False):
        if self.proc is None:
            return
        try:
            self.cmd_q.put_nowait({"type": "stop"})
        except Exception:
            pass
        proc = self.proc
        proc.join(timeout=3 if wait else 0.05)
        if wait and proc.is_alive():
            proc.terminate()
        if not wait:
            self.after(3000, lambda: proc.terminate() if proc.is_alive() else None)
        self.proc = None
        self.running_profile = None
        self.start_btn.configure(text="▶  Start", fg_color=GREEN, hover_color=GREEN_HOVER, text_color="#ffffff")
        self.set_status("Gestoppt.")
        self.stats_label.configure(text="")

    def restart(self):
        self.stop(wait=True)
        self.start()

    def set_status(self, text: str, error: bool = False):
        self.status_label.configure(text=text, text_color="#ff6b6b" if error else ("#1a1a1a", "#e6e8ee"))

    def poll_status(self):
        if self.status_q is not None:
            while True:
                try:
                    msg = self.status_q.get_nowait()
                except (queue.Empty, OSError, ValueError, EOFError):
                    break
                kind = msg.get("type")
                if kind == "started":
                    self.set_status(f"Läuft: „{self.profile['name']}“ · GPU: {msg.get('gpu', '?')}")
                elif kind == "stats":
                    self.stats_label.configure(
                        text=f"Eingang {msg['src']} · {msg['cap_fps']} FPS ({msg['info']})   │   "
                             f"Ausgabe {msg['out_fps']} FPS   │   Latenz {msg['latency']} ms   │   "
                             f"verworfen {msg['dropped']}")
                elif kind in ("error", "warning"):
                    self._had_error = self._had_error or kind == "error"
                    self.set_status(msg.get("msg", ""), error=kind == "error")
                elif kind == "log":
                    print(msg.get("msg", ""), file=sys.stderr)
                elif kind == "closed":
                    self.proc = None
                    self.running_profile = None
                    self.start_btn.configure(text="▶  Start", fg_color=GREEN, hover_color=GREEN_HOVER,
                                             text_color="#ffffff")
                    self.stats_label.configure(text="")
                    if not self._had_error:
                        self.set_status("Ausgabe beendet.")
        if self.proc is not None and not self.proc.is_alive():
            self.proc = None
            self.running_profile = None
            self.start_btn.configure(text="▶  Start", fg_color=GREEN, hover_color=GREEN_HOVER, text_color="#ffffff")
        self.after(150, self.poll_status)

    def on_close(self):
        self.stop(wait=True)
        self._save_now()
        self.destroy()


def run(selftest: bool = False):
    app = App()
    if selftest:
        def finish():
            # Alle Module der Ausgabe einmal laden, damit Packfehler auffallen
            import glfw  # noqa: F401
            import moderngl  # noqa: F401
            import PIL.ImageFont  # noqa: F401
            from . import renderer, shaders  # noqa: F401
            report = (f"video: {app.video_devices}\naudio in: {app.audio_inputs}\n"
                      f"audio out: {app.audio_outputs}\nmonitors: {app.monitors}\n"
                      f"lossless: {lossless.find_lossless('') or '-'}\nSELFTEST OK\n")
            with open("selftest.txt", "w", encoding="utf-8") as fh:
                fh.write(report)
            app.on_close()
        app.after(1500, finish)
    app.mainloop()
