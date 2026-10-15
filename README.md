# MyCampus — Snap 2 Event

A Streamlit campus-events app with poster scanning, OCR, and optional web-based
event corroboration.

## Features

- Upload PNG/JPG/WEBP posters or searchable PDFs, or scan a poster with a camera.
- Extract poster text with Tesseract OCR.
- Search the web and fetch source pages with TinyFish.
- Optionally compare event claims against a selected source with TinyFish Agent.
- Use a local SQLite database by default, or configure MySQL with environment
  variables.

## Setup

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

Image OCR also requires the Tesseract system application. On Windows, install
Tesseract OCR and ensure `tesseract.exe` is on `PATH`. Streamlit Community Cloud
can install it from the included `packages.txt`.

Set `TINYFISH_API_KEY` in the environment to enable TinyFish features. For
Streamlit, it can also be set in `.streamlit/secrets.toml`; do not commit that
file or a real API key.

Run the app:

```bash
streamlit run mycampus_snap2event.py
```

The app creates and seeds `mycampus.db` on first run. The local database is
intentionally excluded from version control.

The event corroboration output is a heuristic summary, not proof that an event
is genuine or fake. Review official organizer, college, venue, or registration
sources before acting.

See [SETUP.md](SETUP.md) for additional setup notes.
