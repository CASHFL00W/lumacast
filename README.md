# LumaCast

Spiel deine Konsole (PlayStation, Xbox, Switch …) über eine Capture-Karte auf dem PC – mit GPU-Upscaling,
Schärfen, Bildverbesserung, Ton-Durchleitung und optionaler Frame Generation über Lossless Scaling.

## Download
Unter **Releases** → `LumaCast.exe` herunterladen und starten. Keine Installation, kein Python nötig.

## Funktionen
- **Capture** von USB- und PCIe-Capture-Karten (DirectShow / Media Foundation), bevorzugt unkomprimierte Formate
  (YUY2/NV12) für geringe Latenz – im Automatik-Modus wird das beste Format selbst gesucht.
- **Hochskalieren auf der GPU:** Pixelgenau, Bilinear, Bikubisch (Catmull-Rom) oder Lanczos mit Halo-Schutz.
- **Schärfen:** kontrastadaptiv (nach AMD CAS), stufenlos 0–100.
- **Bildmodi:** Original, Manuell, Auto (analysiert das Bild laufend), Hybrid. Regler für Helligkeit, Kontrast,
  Sättigung, Dynamik, Gamma, Farbtemperatur, Schwarzwert; HDMI-Farbbereich Voll/Begrenzt.
- **Live-Vorschau:** Regler wirken sofort im laufenden Bild, mit Vergleichsmodus (links Original, rechts bearbeitet).
- **Ton-Durchleitung** von der Capture-Karte zu Lautsprechern/Kopfhörern mit geringer Verzögerung.
- **Profile** (z. B. „PS5 Shooter“, „Switch Retro“) mit allen Einstellungen, Monitor-Wahl und Beschreibung.
- **Frame Generation** über [Lossless Scaling](https://store.steampowered.com/app/993090/Lossless_Scaling/)
  (separat zu kaufen): LumaCast findet es automatisch, startet es mit und löst den Skalier-Hotkey aus.
- **Info-Overlay** mit Eingangs-/Ausgabe-FPS, Latenz und verworfenen Frames.
- **Testbild**, um alles ohne Capture-Karte auszuprobieren.

## Tasten im Ausgabefenster
| Taste | Funktion |
|---|---|
| F1 | Info-Overlay ein/aus |
| F2 | Vergleich: links Original, rechts bearbeitet |
| F3 | Skalierungsverfahren durchschalten |
| F4 | Bildbearbeitung kurz aus |
| F5 | Schärfe in Stufen |
| F11 | Fenster / Vollbild |
| Esc | Ausgabe beenden |

## Tipps
- Capture-Karte an **USB 3** anschließen und vorher OBS o. Ä. schließen.
- Auch für 30-FPS-Spiele **60 FPS** wählen – Konsolen geben fast immer 60 Hz aus.
- Wirkt das Bild flau oder zu dunkel, den **HDMI-Farbbereich** umstellen (Konsole und LumaCast müssen gleich eingestellt sein).
- Für geringste Latenz VSync aus lassen (ideal mit G-Sync/FreeSync).
- Lossless Scaling: Frame Generation auf LSFG stellen, Scaling-Typ „Off“ (LumaCast skaliert bereits).
  Läuft Lossless Scaling als Administrator, LumaCast ebenfalls als Administrator starten, sonst kommt der Hotkey nicht an.

## Selbst starten (Entwickler)
```
pip install -r requirements.txt
python main.py
```
Profile liegen unter `%APPDATA%\LumaCast\profiles.json`.
