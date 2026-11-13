"""Ton der Capture-Karte live an die Lautsprecher/Kopfhörer durchreichen."""
from __future__ import annotations

import collections
import threading

import numpy as np

from .config import DEFAULT_AUDIO

try:
    import sounddevice as sd
except Exception:  # PortAudio fehlt
    sd = None

PREFERRED_APIS = ("Windows WASAPI", "Windows DirectSound", "MME")


def _apis():
    if sd is None:
        return []
    return [a["name"] for a in sd.query_hostapis()]


def _preferred_api_index() -> int | None:
    if sd is None:
        return None
    names = _apis()
    for pref in PREFERRED_APIS:
        if pref in names:
            return names.index(pref)
    return None


def list_devices(kind: str) -> list[str]:
    """kind = 'input' oder 'output'. Liefert Gerätenamen einer Host-API (bevorzugt WASAPI)."""
    if sd is None:
        return []
    api = _preferred_api_index()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    out = []
    try:
        for dev in sd.query_devices():
            if dev[key] > 0 and (api is None or dev["hostapi"] == api):
                if dev["name"] not in out:
                    out.append(dev["name"])
    except Exception:
        pass
    return out


def _find(name: str, kind: str):
    if sd is None or not name or name == DEFAULT_AUDIO:
        return None
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    api = _preferred_api_index()
    devices = list(enumerate(sd.query_devices()))
    for want_api in (api, None):
        for i, dev in devices:
            if dev[key] > 0 and dev["name"] == name and (want_api is None or dev["hostapi"] == want_api):
                return i
    return None


def guess_capture_audio(video_name: str, inputs: list[str]) -> str:
    """Versucht das zum Capture-Gerät passende Audiogerät zu finden."""
    if not video_name:
        return ""
    words = [w.lower() for w in video_name.replace("(", " ").replace(")", " ").split() if len(w) > 2]
    for name in inputs:
        low = name.lower()
        if any(w in low for w in words):
            return name
    for name in inputs:
        low = name.lower()
        if any(k in low for k in ("capture", "hdmi", "elgato", "avermedia", "cam link", "usb video", "digital audio interface")):
            return name
    return ""


class AudioPassthrough:
    BLOCK = 256

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.volume = max(0.0, cfg.get("volume", 100) / 100.0)
        self.buf = collections.deque()
        self.buffered = 0
        self.lock = threading.Lock()
        self.in_stream = None
        self.out_stream = None
        self.error = ""
        self.in_rate = 48000
        self.out_rate = 48000
        self.channels = 2

    def set_volume(self, vol: int):
        self.volume = max(0.0, vol / 100.0)

    def start(self):
        if sd is None:
            self.error = "Audio nicht verfügbar (PortAudio fehlt)."
            return
        in_dev = _find(self.cfg.get("input", ""), "input")
        if in_dev is None:
            self.error = "Kein Audio-Eingang gewählt." if not self.cfg.get("input") else \
                f"Audio-Eingang „{self.cfg.get('input')}“ nicht gefunden."
            return
        out_dev = _find(self.cfg.get("output", ""), "output")
        try:
            in_info = sd.query_devices(in_dev)
            out_info = sd.query_devices(out_dev if out_dev is not None else sd.default.device[1])
            self.in_ch = max(1, min(2, int(in_info["max_input_channels"])))
            self.in_rate = int(in_info["default_samplerate"])
            self.out_rate = int(out_info["default_samplerate"])
            # Gleiche Rate bevorzugen, wenn beide Seiten 48 kHz können
            for rate in (48000, self.in_rate):
                try:
                    sd.check_input_settings(device=in_dev, channels=self.in_ch, samplerate=rate)
                    sd.check_output_settings(device=out_dev, channels=2, samplerate=rate)
                    self.in_rate = self.out_rate = rate
                    break
                except Exception:
                    continue
            self.max_buffer = int(self.out_rate * 0.06)  # max. 60 ms puffern
            self.in_stream = sd.InputStream(device=in_dev, channels=self.in_ch, samplerate=self.in_rate,
                                            blocksize=self.BLOCK, latency="low", dtype="float32",
                                            callback=self._in_cb)
            self.out_stream = sd.OutputStream(device=out_dev, channels=2, samplerate=self.out_rate,
                                              blocksize=self.BLOCK, latency="low", dtype="float32",
                                              callback=self._out_cb)
            self.in_stream.start()
            self.out_stream.start()
        except Exception as exc:  # noqa: BLE001
            self.error = f"Audio-Fehler: {exc}"
            self.stop()

    # ------------------------------------------------------------ Callbacks
    def _in_cb(self, indata, frames, _time, _status):
        data = indata.copy()
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        if self.in_rate != self.out_rate:
            n_out = max(1, int(round(len(data) * self.out_rate / self.in_rate)))
            x_old = np.linspace(0, 1, len(data), endpoint=False)
            x_new = np.linspace(0, 1, n_out, endpoint=False)
            data = np.stack([np.interp(x_new, x_old, data[:, c]) for c in range(2)], axis=1).astype(np.float32)
        with self.lock:
            self.buf.append(data)
            self.buffered += len(data)
            # Latenz begrenzen: zu viel Puffer -> älteste Blöcke verwerfen
            while self.buffered > self.max_buffer and len(self.buf) > 1:
                self.buffered -= len(self.buf.popleft())

    def _out_cb(self, outdata, frames, _time, _status):
        filled = 0
        with self.lock:
            while filled < frames and self.buf:
                block = self.buf[0]
                take = min(frames - filled, len(block))
                outdata[filled:filled + take] = block[:take]
                filled += take
                if take < len(block):
                    self.buf[0] = block[take:]
                else:
                    self.buf.popleft()
                self.buffered -= take
        if filled < frames:
            outdata[filled:] = 0
        if self.volume != 1.0:
            outdata *= self.volume

    def stop(self):
        for stream in (self.in_stream, self.out_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        self.in_stream = self.out_stream = None
