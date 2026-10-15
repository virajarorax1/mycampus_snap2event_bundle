# MyCampus — Snap 2 Event setup

This version adds a **📸 Snap 2 Event** page to the existing Streamlit app.

## Features
- Upload PNG/JPG/WEBP posters or searchable PDFs.
- Scan a poster using the camera input.
- OCR images using Tesseract.
- Search the live web with TinyFish Search and fetch source pages with TinyFish Fetch.
- Show a conservative corroboration label and source links.
- Optionally run a TinyFish Agent comparison against one selected source. Agent usage may be metered.

## Install Python dependencies
Install the included `requirements.txt`:

```bash
pip install -r requirements.txt
```

## Install the Tesseract system application
`pytesseract` is a Python wrapper; it does not install the OCR engine itself.

- **Windows:** install Tesseract OCR, then ensure `tesseract.exe` is on PATH. If needed, configure `pytesseract.pytesseract.tesseract_cmd` in the Python file to the installed executable path.
- **Streamlit Community Cloud:** add the included `packages.txt` (containing `tesseract-ocr`) alongside the app entry point.
- **Scanned PDFs:** this version currently supports PDFs with selectable text. If a PDF is image-only, export its pages as images and upload them, or add PDF-to-image conversion in a later iteration.

## Configure TinyFish
Set `TINYFISH_API_KEY` in the environment, or add it to `.streamlit/secrets.toml`:

```toml
TINYFISH_API_KEY = "your-key-here"
```

Never commit secrets.toml or a real API key. Add `.streamlit/secrets.toml` to `.gitignore`.

## Run
```bash
streamlit run mycampus_snap2event.py
```

## Verification caveat
The automatic result is a heuristic evidence summary. It is **not** proof that an event is genuine or fake. The optional Agent check compares claims against a selected page; review official organizer/university sources and the cited pages before acting. Missing evidence is reported as unable to verify, not as fake.

## Notes
- TinyFish Search: `GET https://api.search.tinyfish.ai?query=...`
- TinyFish Fetch: `POST https://api.fetch.tinyfish.ai` with `{"urls": [...], "format": "markdown"}`
- Optional TinyFish Agent: `POST https://agent.tinyfish.ai/v1/automation/run`
