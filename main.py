"""LumaCast – Konsole über Capture-Karte mit GPU-Upscaling, Bildverbesserung und Frame Gen."""
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()
    from lumacast.gui import run
    run(selftest="--selftest" in sys.argv)


if __name__ == "__main__":
    main()
