"""Bildaufnahme von der Capture-Karte (DirectShow / Media Foundation)."""
from __future__ import annotations

import sys
import threading
import time

import cv2
import numpy as np

from .config import TEST_PATTERN

FORMATS = {"YUY2": "YUY2", "NV12": "NV12", "MJPG": "MJPG"}

FALLBACK_MODE = (1920, 1080, 60)

LOST_SIGNAL = ("Kein Bild von der Capture-Karte – verbinde neu … Prüfen: Konsole an? Karte von einem "
               "anderen Programm belegt (OBS, Kamera-App, zweites LumaCast)? HDCP an der PS5 aus?")


def list_video_devices() -> list[str]:
    """Namen der Videogeräte in DirectShow-Reihenfolge (= OpenCV-Index)."""
    if sys.platform != "win32":
        return []
    try:
        from pygrabber.dshow_graph import FilterGraph
        return list(FilterGraph().get_input_devices())
    except Exception:
        return []


class TestPattern:
    """Bewegtes Testbild, damit man ohne Capture-Karte alles ausprobieren kann."""

    def __init__(self, width: int, height: int, fps: int):
        self.width, self.height, self.fps = width, height, max(1, fps)
        self.t0 = time.perf_counter()
        self.next_t = self.t0
        self.base = self._make_base()

    def _make_base(self) -> np.ndarray:
        w, h = self.width, self.height
        x = np.linspace(0, 1, w, dtype=np.float32)
        y = np.linspace(0, 1, h, dtype=np.float32)[:, None]
        img = np.zeros((h, w, 3), np.float32)
        img[..., 0] = 0.5 + 0.5 * np.sin(6.0 * x + 2.0 * y)        # B
        img[..., 1] = 0.5 + 0.5 * np.sin(4.0 * y + 1.0)            # G
        img[..., 2] = 0.5 + 0.5 * np.cos(5.0 * x - 3.0 * y)        # R
        img = (img * 140 + 40).astype(np.uint8)
        # Farbbalken unten
        bars = [(255, 255, 255), (0, 255, 255), (255, 255, 0), (0, 255, 0),
                (255, 0, 255), (0, 0, 255), (255, 0, 0), (16, 16, 16)]
        bw = w // len(bars)
        y0 = int(h * 0.78)
        for i, col in enumerate(bars):
            img[y0:, i * bw:(i + 1) * bw] = col
        # feine Linien zum Beurteilen der Schärfe
        for i in range(0, int(w * 0.25), 4):
            img[int(h * 0.1):int(h * 0.3), int(w * 0.05) + i] = 235
        # Grauverlauf
        ramp = np.linspace(0, 255, int(w * 0.5)).astype(np.uint8)
        img[int(h * 0.68):int(h * 0.75), int(w * 0.45):int(w * 0.45) + ramp.size] = ramp[None, :, None]
        scale = h / 1080
        cv2.putText(img, "LumaCast Testbild", (int(w * 0.05), int(h * 0.5)),
                    cv2.FONT_HERSHEY_DUPLEX, 2.4 * scale, (250, 250, 250), max(1, int(3 * scale)), cv2.LINE_AA)
        cv2.putText(img, f"{w}x{h} @ {self.fps} FPS", (int(w * 0.05), int(h * 0.6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2 * scale, (230, 230, 230), max(1, int(2 * scale)), cv2.LINE_AA)
        return img

    def read(self):
        self.next_t += 1.0 / self.fps
        delay = self.next_t - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            self.next_t = time.perf_counter()
        t = time.perf_counter() - self.t0
        frame = self.base.copy()
        h, w = frame.shape[:2]
        size = int(h * 0.12)
        cx = int((0.5 + 0.42 * np.sin(t * 1.3)) * (w - size))
        cy = int(h * 0.36)
        cv2.rectangle(frame, (cx, cy), (cx + size, cy + size), (60, 200, 255), -1)
        cv2.circle(frame, (cx + size // 2, cy + size // 2), size // 4, (30, 30, 30), -1)
        return True, frame

    def release(self):
        pass


def _open_device(index: int, width: int, height: int, fps: int, fourcc: str | None):
    backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF] if sys.platform == "win32" else [cv2.CAP_ANY]
    for backend in backends:
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap.release()
            continue
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        ok, frame = cap.read()
        if ok and frame is not None:
            return cap, frame
        cap.release()
    return None, None


def _measure_fps(cap, frames: int = 15) -> float:
    t0 = time.perf_counter()
    n = 0
    for _ in range(frames):
        ok, _f = cap.read()
        if not ok:
            break
        n += 1
    dt = time.perf_counter() - t0
    return n / dt if dt > 0 else 0.0


class CaptureThread(threading.Thread):
    """Liest Frames so schnell wie möglich und behält immer nur das neueste."""

    def __init__(self, video_cfg: dict):
        super().__init__(daemon=True, name="capture")
        self.cfg = dict(video_cfg)
        self.lock = threading.Lock()
        self.new_frame = threading.Event()
        self.frame = None
        self.frame_time = 0.0
        self.seq = 0
        self.running = True
        self.error = ""
        self.info = "Starte …"
        self.fps = 0.0
        self.size = (0, 0)
        self._source = None
        self.use_fallback = False
        self.notice = ""
        self.frames_this_session = 0

    # ---------------------------------------------------------------- öffnen
    def _open(self):
        c = self.cfg
        w, h, fps = int(c["width"]), int(c["height"]), int(c["fps"])
        name = c.get("device") or ""
        if self.use_fallback and (w, h, fps) != FALLBACK_MODE:
            self.notice = (f"Die Capture-Karte liefert bei {w}x{h} @ {fps} FPS kein Bild – "
                           f"LumaCast nutzt stattdessen {FALLBACK_MODE[0]}x{FALLBACK_MODE[1]} @ "
                           f"{FALLBACK_MODE[2]} FPS. Bitte im Reiter Video anpassen.")
            w, h, fps = FALLBACK_MODE
        if name == TEST_PATTERN or (sys.platform != "win32" and not name):
            self.info = "Testbild"
            return TestPattern(w, h, fps)

        devices = list_video_devices()
        index = devices.index(name) if name in devices else 0
        if not devices and sys.platform == "win32":
            raise RuntimeError("Keine Capture-Karte gefunden – ist sie angeschlossen (USB 3)?")

        wanted = c.get("format", "Auto")
        candidates = [wanted] if wanted in FORMATS else ["YUY2", "NV12", "MJPG", None]
        best = None
        for fmt in candidates:
            cap, frame = _open_device(index, w, h, fps, fmt)
            if cap is None:
                continue
            fh, fw = frame.shape[:2]
            measured = _measure_fps(cap) if len(candidates) > 1 else fps
            good = (fw, fh) == (w, h) and measured >= fps * 0.85
            label = fmt or "Standard"
            if good or len(candidates) == 1:
                self.info = f"{label} {fw}x{fh}"
                if best:
                    best[0].release()
                return cap
            score = (fw * fh, measured)
            if best is None or score > best[2]:
                if best:
                    best[0].release()
                best = (cap, label, score, (fw, fh))
            else:
                cap.release()
        if best:
            self.info = f"{best[1]} {best[3][0]}x{best[3][1]}"
            return best[0]
        if not self.use_fallback and (w, h, fps) != FALLBACK_MODE:
            self.use_fallback = True
            return self._open()
        raise RuntimeError(f"Capture-Gerät „{name or index}“ konnte nicht geöffnet werden – "
                           "evtl. noch in OBS, der Kamera-App oder einem zweiten LumaCast geöffnet")

    # ---------------------------------------------------------------- Schleife
    def _release(self):
        if self._source is not None:
            try:
                self._source.release()
            except Exception:
                pass
        self._source = None

    def _set_error(self, msg: str):
        self.error = msg
        self.fps = 0.0
        self.new_frame.set()

    def run(self):
        """Liest dauerhaft Bilder. Bricht das Signal ab, wird die Karte automatisch neu geöffnet."""
        attempt = 0
        while self.running:
            try:
                self._source = self._open()
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                self._set_error(f"{exc} (neuer Versuch läuft …)")
                self._sleep(2.0)
                continue
            attempt = 0
            if self.error:
                self.error = ""
                self.new_frame.set()
            self.frames_this_session = 0
            self._read_loop()
            self._release()
            if self.running and self.frames_this_session == 0 and not self.use_fallback:
                # Modus wird angenommen, liefert aber nichts -> nächstes Mal 1080p60
                self.use_fallback = True
                continue
            if self.running:
                self._set_error(LOST_SIGNAL)
                self._sleep(1.5)

    def _sleep(self, seconds: float):
        end = time.perf_counter() + seconds
        while self.running and time.perf_counter() < end:
            time.sleep(0.05)

    def _read_loop(self):
        last = time.perf_counter()
        count = 0
        fails = 0
        while self.running:
            ok, frame = self._source.read()
            now = time.perf_counter()
            if not ok or frame is None:
                fails += 1
                if fails > 120:
                    return
                time.sleep(0.005)
                continue
            fails = 0
            self.frames_this_session += 1
            if self.error:
                self.error = ""
            with self.lock:
                self.frame = frame
                self.frame_time = now
                self.seq += 1
                self.size = (frame.shape[1], frame.shape[0])
            self.new_frame.set()
            count += 1
            if now - last >= 0.5:
                self.fps = count / (now - last)
                count = 0
                last = now

    def latest(self):
        with self.lock:
            return self.frame, self.frame_time, self.seq

    def stop(self):
        self.running = False
