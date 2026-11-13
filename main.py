"""LumaCast – Konsole über Capture-Karte mit GPU-Upscaling, Bildverbesserung und Frame Gen."""
import multiprocessing
import sys


_MUTEX = None


def single_instance() -> bool:
    """Verhindert, dass LumaCast doppelt läuft (zwei Instanzen blockieren die Capture-Karte)."""
    global _MUTEX
    if sys.platform != "win32" or "--selftest" in sys.argv:
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    import time
    # Nach Neustart (Update/Admin) kurz warten, bis die alte Instanz beendet ist
    deadline = time.time() + (10 if "--restarted" in sys.argv else 0)
    while True:
        _MUTEX = kernel32.CreateMutexW(None, False, "Local\\LumaCast.SingleInstance")
        if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            return True
        kernel32.CloseHandle(_MUTEX)
        if time.time() >= deadline:
            break
        time.sleep(0.3)
    # Läuft schon: vorhandenes Fenster nach vorne holen, falls möglich
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, None)
    title = ctypes.create_unicode_buffer(256)
    while hwnd:
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value.startswith("LumaCast ") and "Ausgabe" not in title.value:
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            break
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    user32.MessageBoxW(None, "LumaCast läuft bereits.", "LumaCast", 0x40)
    return False


def main():
    multiprocessing.freeze_support()
    if not single_instance():
        return
    from lumacast.gui import run
    run(selftest="--selftest" in sys.argv)


if __name__ == "__main__":
    main()
