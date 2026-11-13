"""Ausgabefenster: nimmt Frames von der Capture-Karte und rendert sie per GPU."""
from __future__ import annotations

import copy
import math
import queue
import sys
import threading
import time

import numpy as np

from . import lossless, shaders
from .config import APP_NAME

UPSCALERS = ["nearest", "bilinear", "bicubic", "lanczos"]
UPSCALER_LABELS = {"nearest": "Pixelgenau", "bilinear": "Bilinear", "bicubic": "Bikubisch", "lanczos": "Lanczos"}
MODES = {"original": 0, "manual": 1, "auto": 2, "hybrid": 3}
MODE_LABELS = {"original": "Original", "manual": "Manuell", "auto": "Auto", "hybrid": "Hybrid"}


# ---------------------------------------------------------------------------
def setu(prog, name, value):
    """Setzt eine Uniform, falls der Shader-Compiler sie nicht wegoptimiert hat."""
    try:
        prog[name].value = value
    except KeyError:
        pass


def manual_uniforms(img: dict) -> dict:
    t = img.get("temperature", 0) / 100.0
    return {
        "brightness": img.get("brightness", 0) / 100.0 * 0.25,
        "contrast": 1.0 + img.get("contrast", 0) / 100.0 * 0.8,
        "saturation": 1.0 + img.get("saturation", 0) / 100.0,
        "vibrance": img.get("vibrance", 0) / 100.0,
        "gamma_exp": 2.0 ** (-img.get("gamma", 0) / 100.0),
        "tint": (1.0 + 0.10 * t, 1.0 + 0.02 * t, 1.0 - 0.10 * t),
        "black": img.get("black_level", 0) / 100.0 * 0.12,
    }


class AutoTuner:
    """Analysiert das Bild regelmäßig und passt Schwarz-/Weißpunkt und Gamma weich an."""

    def __init__(self):
        self.black, self.white, self.gamma = 0.0, 1.0, 1.0
        self.counter = 0

    def update(self, frame: np.ndarray, limited: bool, strength: float):
        self.counter += 1
        if self.counter % 4:
            return
        import cv2
        small = cv2.resize(frame, (128, 72), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        luma = small[..., 2] * 0.2126 + small[..., 1] * 0.7152 + small[..., 0] * 0.0722
        if limited:
            luma = np.clip((luma - 16 / 255) * (255 / 219), 0, 1)
        lo, hi = np.percentile(luma, (0.5, 99.5))
        black = float(np.clip(lo, 0.0, 0.12))
        white = float(np.clip(hi, 0.65, 1.0))
        stretched = np.clip((luma - black) / max(white - black, 0.05), 1e-4, 1)
        mean = float(np.clip(stretched.mean(), 0.05, 0.95))
        gamma = float(np.clip(math.log(0.45) / math.log(mean), 0.75, 1.35))
        s = strength
        target = (black * s, 1.0 - (1.0 - white) * s, 1.0 + (gamma - 1.0) * s)
        a = 0.08
        self.black += (target[0] - self.black) * a
        self.white += (target[1] - self.white) * a
        self.gamma += (target[2] - self.gamma) * a


class DuplicateDetector:
    """Erkennt doppelte Frames (z. B. 30-FPS-Spiel in 60-Hz-Signal)."""

    def __init__(self):
        self.prev = None

    def is_duplicate(self, frame: np.ndarray) -> bool:
        import cv2
        small = cv2.resize(frame, (240, 135), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        prev, self.prev = self.prev, gray
        if prev is None or prev.shape != gray.shape:
            return False
        changed = int(np.count_nonzero(cv2.absdiff(gray, prev) > 10))
        return changed < 3


def dest_rect(mode: str, src_w: int, src_h: int, win_w: int, win_h: int):
    """Liefert (x, y, w, h) des Bildbereichs und uv-Rechteck (Zuschnitt)."""
    uv = (0.0, 0.0, 1.0, 1.0)
    if src_w <= 0 or src_h <= 0 or win_w <= 0 or win_h <= 0:
        return (0, 0, win_w, win_h), uv
    if mode == "stretch":
        return (0, 0, win_w, win_h), uv
    src_ar, win_ar = src_w / src_h, win_w / win_h
    if mode == "fill":
        if src_ar > win_ar:
            frac = win_ar / src_ar
            uv = ((1 - frac) / 2, 0.0, frac, 1.0)
        else:
            frac = src_ar / win_ar
            uv = (0.0, (1 - frac) / 2, 1.0, frac)
        return (0, 0, win_w, win_h), uv
    if mode == "integer":
        k = max(1, min(win_w // src_w, win_h // src_h))
        w, h = src_w * k, src_h * k
        if w > win_w or h > win_h:
            mode = "fit"
        else:
            return ((win_w - w) // 2, (win_h - h) // 2, w, h), uv
    # fit
    if src_ar > win_ar:
        w, h = win_w, round(win_w / src_ar)
    else:
        w, h = round(win_h * src_ar), win_h
    return ((win_w - w) // 2, (win_h - h) // 2, w, h), uv


# ---------------------------------------------------------------------------
class TextOverlay:
    def __init__(self, ctx):
        from PIL import ImageFont
        self.ctx = ctx
        self.tex = None
        self.size = (0, 0)
        self.font = None
        for name in ("consola.ttf", "segoeui.ttf", "arial.ttf", "DejaVuSansMono.ttf"):
            try:
                self.font = ImageFont.truetype(name, 18)
                break
            except OSError:
                continue
        if self.font is None:
            self.font = ImageFont.load_default()

    def set_text(self, lines: list[str]):
        from PIL import Image, ImageDraw
        if not lines:
            self.size = (0, 0)
            return
        pad, line_h = 12, 24
        tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        width = max(int(tmp.textlength(line, font=self.font)) for line in lines) + pad * 2
        height = line_h * len(lines) + pad * 2 - 4
        img = Image.new("RGBA", (width, height), (10, 12, 18, 190))
        draw = ImageDraw.Draw(img)
        for i, line in enumerate(lines):
            color = (255, 214, 102, 255) if i == 0 else (235, 238, 245, 255)
            draw.text((pad, pad + i * line_h), line, font=self.font, fill=color)
        if self.tex is None or self.tex.size != img.size:
            if self.tex is not None:
                self.tex.release()
            self.tex = self.ctx.texture(img.size, 4)
        self.tex.write(img.tobytes())
        self.size = img.size


# ---------------------------------------------------------------------------
class Renderer:
    def __init__(self, profile: dict, cmd_q, status_q):
        self.profile = copy.deepcopy(profile)
        self.cmd_q = cmd_q
        self.status_q = status_q
        self.compare = False
        self.force_original = False
        self.overlay_on = bool(self.profile["output"].get("overlay"))
        self.toast = ("", 0.0)
        self.dirty = True
        self.running = True
        self.windowed = False
        self.autotune = AutoTuner()
        self.dups = DuplicateDetector()
        self.audio = None
        self.hotkey_at = None
        self.hotkey_pending = False
        self.overlay_dirty = True

    # ----------------------------------------------------------------- Status
    def send(self, kind: str, **data):
        try:
            self.status_q.put_nowait({"type": kind, **data})
        except Exception:
            pass

    def show_toast(self, text: str):
        self.toast = (text, time.perf_counter() + 1.8)
        self.overlay_dirty = True
        self.dirty = True

    # ------------------------------------------------------------------ Setup
    def setup_window(self):
        import glfw
        if not glfw.init():
            raise RuntimeError("GLFW konnte nicht initialisiert werden.")
        monitors = glfw.get_monitors() or []
        idx = int(self.profile["output"].get("monitor", 0))
        mon = monitors[idx] if 0 <= idx < len(monitors) else (monitors[0] if monitors else None)
        if mon is not None:
            mode = glfw.get_video_mode(mon)
            mx, my = glfw.get_monitor_pos(mon)
            mw, mh = mode.size.width, mode.size.height
        else:
            mx, my, mw, mh = 0, 0, 1280, 720
        self.monitor_rect = (mx, my, mw, mh)

        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        fullscreen = bool(self.profile["output"].get("start_fullscreen", False))
        self.windowed = not fullscreen
        ww, wh = self._window_size()
        glfw.window_hint(glfw.DECORATED, not fullscreen)
        glfw.window_hint(glfw.RESIZABLE, True)
        glfw.window_hint(glfw.AUTO_ICONIFY, False)
        glfw.window_hint(glfw.FOCUS_ON_SHOW, True)
        glfw.window_hint(glfw.VISIBLE, False)
        size = (mw, mh) if fullscreen else (ww, wh)
        win = glfw.create_window(size[0], size[1], f"{APP_NAME} – Ausgabe", None, None)
        if not win:
            raise RuntimeError("Ausgabefenster konnte nicht erstellt werden (OpenGL 3.3 nötig).")
        if fullscreen:
            glfw.set_window_pos(win, mx, my)
        else:
            glfw.set_window_pos(win, mx + (mw - ww) // 2, my + (mh - wh) // 2)
        glfw.make_context_current(win)
        glfw.swap_interval(1 if self.profile["output"].get("vsync") else 0)
        glfw.set_input_mode(win, glfw.CURSOR, glfw.CURSOR_HIDDEN if fullscreen else glfw.CURSOR_NORMAL)
        glfw.set_key_callback(win, self.on_key)
        glfw.set_mouse_button_callback(win, self.on_mouse)
        glfw.set_framebuffer_size_callback(win, lambda *_: self._mark_resize())
        glfw.show_window(win)
        glfw.focus_window(win)
        self.glfw = glfw
        self.win = win

    def _window_size(self):
        """16:9-Fenster mit ca. 70 % der Monitorbreite."""
        _mx, _my, mw, mh = self.monitor_rect
        w = int(mw * 0.7)
        h = int(w * 9 / 16)
        if h > mh * 0.8:
            h = int(mh * 0.8)
            w = int(h * 16 / 9)
        return w, h

    def on_mouse(self, win, button, action, _mods):
        glfw = self.glfw
        if button != glfw.MOUSE_BUTTON_LEFT or action != glfw.PRESS:
            return
        now = time.perf_counter()
        if now - getattr(self, "_last_click", 0.0) < 0.35:
            self._last_click = 0.0
            self.toggle_windowed()
            self.dirty = True
        else:
            self._last_click = now

    def _mark_resize(self):
        self.dirty = True

    def setup_gl(self):
        import moderngl
        self.mgl = moderngl
        ctx = moderngl.create_context()
        self.ctx = ctx
        self.gpu_name = ctx.info.get("GL_RENDERER", "?")
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")
        self.vbo = ctx.buffer(quad.tobytes())

        def prog(frag):
            p = ctx.program(vertex_shader=shaders.VERTEX, fragment_shader=frag)
            vao = ctx.vertex_array(p, [(self.vbo, "2f", "in_pos")])
            return p, vao

        self.p_color, self.v_color = prog(shaders.COLOR)
        self.p_up, self.v_up = prog(shaders.UPSCALE)
        self.p_sharp, self.v_sharp = prog(shaders.SHARPEN)
        self.p_ovl, self.v_ovl = prog(shaders.OVERLAY)
        self.src_tex = None
        self.color_tex = self.color_fbo = None
        self.up_tex = self.up_fbo = None
        self.text = TextOverlay(ctx)
        self.overlay_dirty = True

    def ensure_src(self, w: int, h: int):
        if self.src_tex is not None and self.src_tex.size == (w, h):
            return
        for obj in (self.src_tex, self.color_tex, self.color_fbo):
            if obj is not None:
                obj.release()
        mgl = self.mgl
        self.src_tex = self.ctx.texture((w, h), 3, dtype="f1", alignment=1)
        self.src_tex.filter = (mgl.LINEAR, mgl.LINEAR)
        self.color_tex = self.ctx.texture((w, h), 4, dtype="f2")
        self.color_tex.filter = (mgl.LINEAR, mgl.LINEAR)
        self.color_tex.repeat_x = self.color_tex.repeat_y = False
        self.color_fbo = self.ctx.framebuffer(color_attachments=[self.color_tex])

    def ensure_up(self, w: int, h: int):
        if self.up_tex is not None and self.up_tex.size == (w, h):
            return
        for obj in (self.up_tex, self.up_fbo):
            if obj is not None:
                obj.release()
        self.up_tex = self.ctx.texture((w, h), 4, dtype="f2")
        self.up_tex.filter = (self.mgl.NEAREST, self.mgl.NEAREST)
        self.up_fbo = self.ctx.framebuffer(color_attachments=[self.up_tex])

    # ---------------------------------------------------------------- Hotkeys
    def on_key(self, win, key, _scancode, action, mods):
        glfw = self.glfw
        if action != glfw.PRESS:
            return
        out = self.profile["output"]
        if key == glfw.KEY_ESCAPE and out.get("esc_closes", True):
            glfw.set_window_should_close(win, True)
        elif key == glfw.KEY_F1:
            self.overlay_on = not self.overlay_on
            self.overlay_dirty = True
        elif key == glfw.KEY_F2:
            self.compare = not self.compare
            self.show_toast("Vergleich: links Original | rechts bearbeitet" if self.compare else "Vergleich aus")
        elif key == glfw.KEY_F3:
            up = self.profile["upscale"]
            i = UPSCALERS.index(up.get("method", "lanczos")) if up.get("method") in UPSCALERS else 3
            up["method"] = UPSCALERS[(i + 1) % len(UPSCALERS)]
            self.show_toast(f"Skalierung: {UPSCALER_LABELS[up['method']]}")
        elif key == glfw.KEY_F4:
            self.force_original = not self.force_original
            mode = "original" if self.force_original else self.profile["image"].get("mode", "original")
            self.show_toast(f"Bildmodus: {MODE_LABELS.get(mode, mode)}")
        elif key == glfw.KEY_F5:
            up = self.profile["upscale"]
            steps = [0, 25, 50, 75, 100]
            cur = up.get("sharpness", 40)
            nxt = next((s for s in steps if s > cur), 0)
            up["sharpness"] = nxt
            self.show_toast(f"Schärfe: {nxt}")
        elif key == glfw.KEY_F11:
            self.toggle_windowed()
        self.dirty = True

    def toggle_windowed(self):
        glfw = self.glfw
        mx, my, mw, mh = self.monitor_rect
        self.windowed = not self.windowed
        if self.windowed:
            w, h = self._window_size()
            glfw.set_window_attrib(self.win, glfw.DECORATED, True)
            glfw.set_window_size(self.win, w, h)
            glfw.set_window_pos(self.win, mx + (mw - w) // 2, my + (mh - h) // 2)
            glfw.set_input_mode(self.win, glfw.CURSOR, glfw.CURSOR_NORMAL)
        else:
            glfw.set_window_attrib(self.win, glfw.DECORATED, False)
            glfw.set_window_pos(self.win, mx, my)
            glfw.set_window_size(self.win, mw, mh)
            glfw.set_input_mode(self.win, glfw.CURSOR, glfw.CURSOR_HIDDEN)

    # ----------------------------------------------------------------- Render
    def render(self, frame: np.ndarray | None):
        ctx = self.ctx
        win_w, win_h = self.glfw.get_framebuffer_size(self.win)
        if win_w == 0 or win_h == 0:
            return
        screen = ctx.screen
        screen.use()
        ctx.viewport = (0, 0, win_w, win_h)
        ctx.clear(0.0, 0.0, 0.0, 1.0)
        if frame is not None:
            h, w = frame.shape[:2]
            self.ensure_src(w, h)
            if not frame.flags["C_CONTIGUOUS"]:
                frame = np.ascontiguousarray(frame)
            self.src_tex.write(frame)

        if self.src_tex is not None:
            w, h = self.src_tex.size
            img = self.profile["image"]
            up = self.profile["upscale"]
            out = self.profile["output"]
            mode_name = "original" if self.force_original else img.get("mode", "original")
            split = 0.5 if self.compare else -1.0

            # Pass 1: Farbe
            self.color_fbo.use()
            ctx.viewport = (0, 0, w, h)
            p = self.p_color
            self.src_tex.use(0)
            setu(p, "src", 0)
            setu(p, "uv_rect", (0.0, 0.0, 1.0, 1.0))
            setu(p, "bgr", True)
            setu(p, "limited_range", img.get("input_range") == "limited")
            setu(p, "mode", MODES.get(mode_name, 0))
            setu(p, "compare_split", split)
            for k, v in manual_uniforms(img).items():
                setu(p, k, v)
            setu(p, "auto_black", self.autotune.black)
            setu(p, "auto_white", self.autotune.white)
            setu(p, "auto_gamma", self.autotune.gamma)
            self.v_color.render(self.mgl.TRIANGLE_STRIP)

            # Pass 2: Skalieren
            rect, uv = dest_rect(out.get("scale_mode", "fit"), w, h, win_w, win_h)
            sharp = up.get("sharpness", 0)
            if sharp > 0:
                self.ensure_up(win_w, win_h)
                self.up_fbo.use()
                ctx.viewport = (0, 0, win_w, win_h)
                ctx.clear(0.0, 0.0, 0.0, 1.0)
            else:
                screen.use()
            ctx.viewport = rect
            p = self.p_up
            self.color_tex.use(0)
            setu(p, "src", 0)
            setu(p, "uv_rect", uv)
            setu(p, "src_size", (float(w), float(h)))
            method = up.get("method", "lanczos")
            setu(p, "method", UPSCALERS.index(method) if method in UPSCALERS else 3)
            setu(p, "anti_ringing", bool(up.get("anti_ringing", True)))
            self.v_up.render(self.mgl.TRIANGLE_STRIP)

            # Pass 3: Schärfen
            if sharp > 0:
                screen.use()
                ctx.viewport = (0, 0, win_w, win_h)
                p = self.p_sharp
                self.up_tex.use(0)
                setu(p, "src", 0)
                setu(p, "uv_rect", (0.0, 0.0, 1.0, 1.0))
                setu(p, "sharpness", sharp / 100.0)
                setu(p, "compare_split", split)
                setu(p, "dest_rect", tuple(float(x) for x in rect))
                self.v_sharp.render(self.mgl.TRIANGLE_STRIP)

            if self.compare:
                x = rect[0] + rect[2] // 2
                self._fill((x - 1, rect[1], 2, rect[3]), (1.0, 0.85, 0.3, 1.0))

        self._draw_overlay(win_w, win_h)

    def _fill(self, rect, color):
        ctx = self.ctx
        ctx.viewport = rect
        setu(self.p_ovl, "use_tex", False)
        setu(self.p_ovl, "color", color)
        setu(self.p_ovl, "uv_rect", (0.0, 0.0, 1.0, 1.0))
        self.v_ovl.render(self.mgl.TRIANGLE_STRIP)

    def _draw_overlay(self, win_w, win_h):
        if self.text.size == (0, 0) or self.text.tex is None:
            return
        ctx = self.ctx
        tw, th = self.text.size
        ctx.enable(self.mgl.BLEND)
        ctx.blend_func = self.mgl.SRC_ALPHA, self.mgl.ONE_MINUS_SRC_ALPHA
        ctx.viewport = (20, win_h - th - 20, tw, th)
        self.text.tex.use(0)
        setu(self.p_ovl, "src", 0)
        setu(self.p_ovl, "use_tex", True)
        setu(self.p_ovl, "uv_rect", (0.0, 0.0, 1.0, 1.0))
        self.v_ovl.render(self.mgl.TRIANGLE_STRIP)
        ctx.disable(self.mgl.BLEND)
        ctx.viewport = (0, 0, win_w, win_h)

    # -------------------------------------------------------------- Befehle
    def handle_commands(self):
        while True:
            try:
                cmd = self.cmd_q.get_nowait()
            except queue.Empty:
                return
            except Exception:
                return
            kind = cmd.get("type")
            if kind == "stop":
                self.running = False
            elif kind == "ls_hotkey":
                self.hotkey_at = time.perf_counter() + 0.1
            elif kind == "update":
                new = cmd["profile"]
                for section in ("image", "upscale"):
                    self.profile[section] = copy.deepcopy(new[section])
                for key in ("scale_mode", "esc_closes", "skip_duplicates"):
                    self.profile["output"][key] = new["output"].get(key)
                if new["output"].get("overlay") != self.profile["output"].get("overlay"):
                    self.profile["output"]["overlay"] = new["output"].get("overlay")
                    self.overlay_on = bool(new["output"].get("overlay"))
                if self.audio is not None:
                    self.audio.set_volume(new["audio"].get("volume", 100))
                self.force_original = False
                self.dirty = True
                self.overlay_dirty = True

    # ------------------------------------------------------------------- Run
    def run(self):
        from .capture import CaptureThread
        from .audio import AudioPassthrough

        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
                ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x8000)
            except Exception:
                pass

        fg = self.profile["framegen"]
        ls_path = ""
        if fg.get("lossless"):
            ls_path = lossless.find_lossless(fg.get("ls_path", ""))
            err = lossless.launch(ls_path)
            if err:
                self.send("error", msg=err)

        self.setup_window()
        self.setup_gl()
        self.send("started", gpu=self.gpu_name)

        cap = CaptureThread(self.profile["video"])
        cap.start()
        self.audio = None
        audio_note = ""
        if self.profile["audio"].get("enabled") and self.profile["audio"].get("input"):
            self.audio = AudioPassthrough(self.profile["audio"])
            self.audio.start()
            if self.audio.error:
                audio_note = self.audio.error
                self.send("warning", msg=audio_note)

        # Hotkey erst senden, wenn wirklich Bilder kommen (Capture-Start kann dauern)
        self.hotkey_at = None
        self.hotkey_pending = bool(fg.get("lossless") and fg.get("send_hotkey") and fg.get("hotkey"))

        glfw = self.glfw
        last_seq = -1
        out_frames = 0
        unique_frames = 0
        game_fps = 0.0
        dropped = 0
        lat_sum = 0.0
        lat_n = 0
        stat_t = time.perf_counter()
        out_fps = 0.0
        latency_ms = 0.0
        error_shown = ""

        while self.running and not glfw.window_should_close(self.win):
            glfw.poll_events()
            self.handle_commands()

            if self.hotkey_at and time.perf_counter() >= self.hotkey_at:
                self.hotkey_at = None
                self.trigger_lossless()

            cap.new_frame.wait(0.004)
            cap.new_frame.clear()
            frame, ftime, seq = cap.latest()
            new = seq != last_seq and frame is not None

            if cap.error and cap.error != error_shown:
                error_shown = cap.error
                self.send("error", msg=cap.error)
                self.toast = (cap.error, time.perf_counter() + 1e9)
                self.overlay_dirty = True
                self.dirty = True
            elif cap.notice and cap.notice != getattr(self, "_notice_shown", ""):
                self._notice_shown = cap.notice
                self.send("warning", msg=cap.notice)
                self.show_toast("Ersatz-Modus 1080p60 – Einstellung nicht unterstützt")
            elif error_shown and not cap.error:
                error_shown = ""
                self.send("info", msg="Bild von der Capture-Karte ist wieder da.")
                self.toast = ("Signal wieder da", time.perf_counter() + 1.5)
                self.overlay_dirty = True
                self.dirty = True

            if new:
                if last_seq >= 0 and seq - last_seq > 1:
                    dropped += seq - last_seq - 1
                last_seq = seq
                duplicate = self.dups.is_duplicate(frame)
                if not duplicate:
                    unique_frames += 1
                elif self.profile["output"].get("skip_duplicates", True):
                    # Nichts Neues im Bild -> nicht erneut ausgeben. So sieht Lossless Scaling
                    # die echte Spiel-Bildrate (z. B. 30) und berechnet saubere Zwischenbilder.
                    new = False
                img = self.profile["image"]
                if new and not self.force_original and img.get("mode") in ("auto", "hybrid"):
                    self.autotune.update(frame, img.get("input_range") == "limited",
                                         img.get("auto_strength", 60) / 100.0)

            now = time.perf_counter()
            if now - stat_t >= 0.5:
                out_fps = out_frames / (now - stat_t)
                latency_ms = (lat_sum / lat_n * 1000.0) if lat_n else 0.0
                game_fps = unique_frames / (now - stat_t)
                self.game_fps = game_fps
                unique_frames = 0
                out_frames = 0
                lat_sum = 0.0
                lat_n = 0
                stat_t = now
                stats = {
                    "cap_fps": round(cap.fps, 1), "game_fps": round(game_fps, 1), "out_fps": round(out_fps, 1),
                    "latency": round(latency_ms, 1), "dropped": dropped,
                    "src": f"{cap.size[0]}x{cap.size[1]}", "info": cap.info,
                }
                self.send("stats", **stats)
                if self.overlay_on:
                    self.overlay_dirty = True

            if self.toast[0] and now > self.toast[1]:
                self.toast = ("", 0.0)
                self.overlay_dirty = True

            if self.overlay_dirty:
                self.overlay_dirty = False
                self.update_overlay(cap, out_fps, latency_ms, dropped, audio_note)
                self.dirty = True

            if self.hotkey_pending and last_seq >= 0:
                self.hotkey_pending = False
                self.hotkey_at = time.perf_counter() + float(fg.get("delay", 2.0))

            if new or self.dirty:
                self.render(frame if new else None)
                glfw.swap_buffers(self.win)
                if new:
                    lat_sum += time.perf_counter() - ftime
                    lat_n += 1
                out_frames += 1
                self.dirty = False

        cap.stop()
        if self.audio is not None:
            self.audio.stop()
        glfw.destroy_window(self.win)
        glfw.terminate()
        self.send("closed")

    def trigger_lossless(self):
        """Ausgabefenster nach vorne holen und den Skalier-Hotkey von Lossless Scaling drücken."""
        combo = self.profile["framegen"].get("hotkey", "ctrl+alt+s") or "ctrl+alt+s"
        if lossless.hotkey_blocked():
            msg = ("Lossless Scaling läuft als Administrator – Windows blockiert den Hotkey. "
                   "LumaCast als Administrator starten oder im Ausgabefenster selbst "
                   f"{combo.upper()} drücken.")
            self.send("warning", msg=msg)
            self.show_toast("LS läuft als Admin: bitte " + combo.upper() + " selbst drücken")
            return
        if not lossless.is_running():
            self.send("warning", msg="Lossless Scaling läuft nicht – bitte starten.")
            self.show_toast("Lossless Scaling läuft nicht")
            return
        glfw = self.glfw
        glfw.restore_window(self.win)
        glfw.focus_window(self.win)
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = glfw.get_win32_window(self.win)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        threading.Thread(target=lambda: (time.sleep(0.4), lossless.send_hotkey(combo)), daemon=True).start()
        self.show_toast(f"Lossless Scaling: {combo.upper()} gesendet")
        self.send("info", msg=f"{combo.upper()} an Lossless Scaling gesendet. Oben links sollte jetzt "
                              "die LS-FPS-Anzeige erscheinen (falls „Draw FPS“ an ist).")

    def update_overlay(self, cap, out_fps, latency_ms, dropped, audio_note):
        lines = []
        if self.overlay_on:
            win_w, win_h = self.glfw.get_framebuffer_size(self.win)
            up = self.profile["upscale"]
            mode = "original" if self.force_original else self.profile["image"].get("mode", "original")
            lines = [
                f"{APP_NAME}",
                f"Eingang   {cap.size[0]}x{cap.size[1]}  {cap.fps:5.1f} FPS  ({cap.info})",
                f"Spiel     {getattr(self, 'game_fps', 0.0):5.1f} FPS (echte neue Bilder)",
                f"Ausgabe   {win_w}x{win_h}  {out_fps:5.1f} FPS"
                + ("  (Duplikate übersprungen)" if self.profile["output"].get("skip_duplicates", True) else ""),
                f"Latenz    {latency_ms:4.1f} ms (Programm)   verworfen: {dropped}",
                f"Skalierung {UPSCALER_LABELS.get(up.get('method'), '?')}  Schärfe {up.get('sharpness', 0)}",
                f"Bild      {MODE_LABELS.get(mode, mode)}",
                f"GPU       {self.gpu_name}",
                "F1 Info  F2 Vergleich  F3 Skalierung  F4 Original  F5 Schärfe  F11/Doppelklick Vollbild  Esc Ende",
            ]
            if audio_note:
                lines.append(f"Audio     {audio_note}")
        if self.toast[0]:
            if lines:
                lines.append("")
                lines.append(self.toast[0])
            else:
                lines = [self.toast[0]]
        self.text.set_text(lines)


def render_main(profile: dict, cmd_q, status_q):
    """Einstiegspunkt des Ausgabe-Prozesses."""
    renderer = Renderer(profile, cmd_q, status_q)
    try:
        renderer.run()
    except Exception as exc:  # noqa: BLE001
        import traceback
        renderer.send("error", msg=f"{exc}")
        renderer.send("log", msg=traceback.format_exc())
        renderer.send("closed")
