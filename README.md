# Video Downloader Streamlit App

Streamlit front-end for downloading videos with [yt-dlp](https://github.com/yt-dlp/yt-dlp). The app supports one-off downloads as well as batch jobs driven by CSV files, and lets you reuse browser cookies for locked-down sources.

## Virtual Access
- App can be accessed virtually via: https://betterillumis-ocj7zc4myap2gpcmcrebpb.streamlit.app/

## Local Prerequisites (optional)
- Python 3.9 or newer with `pip`
- `ffmpeg` (optional but required for clipping). If you do not have it globally installed, the bundled `imageio-ffmpeg` dependency will try to provision a copy automatically.
- Recommended: a virtual environment (e.g. `python -m venv .venv`)

## Local Setup (optional)
```bash
python -m venv .venv
.venv\Scripts\activate           # PowerShell
# source .venv/bin/activate      # macOS/Linux
pip install --upgrade pip
pip install -r requirements.txt
```

## Locally Run the App (optional)
```bash
streamlit run video_downloader_app.py
```

Streamlit will print a local URL (typically http://localhost:8501); open it in your browser.

Downloads are saved under the `downloads/` folder relative to where you launch Streamlit. The app also offers direct download buttons for completed jobs.

## Single Video Downloads
- Open the **Single Video Download** panel in the sidebar.
- Enter the video URL and an optional filename (omit the extension).
- Optionally supply clip start/end times in `HH:MM:SS`, `MM:SS`, or plain seconds. Clipping requires `ffmpeg`.
- Upload a cookies file if the source needs authentication (use a browser extension such as *Get cookies.txt* to export in Netscape format).
- Submit the form. While running you will see a spinner; when finished the app shows the saved file path, an inline download button, and captured logs.

## Batch Downloads from CSV
1. Prepare a CSV with a header row that includes `URL`, `Link`, or `Links`.
2. Optional columns:
   - `Download Status`, `Download Detail`, `Download Path`, `Processed At` — the app writes or updates these; if they are missing they are added automatically.
   - `Skip` — rows with `1/true/yes/skip` are ignored.
3. In the **Batch Downloads** sidebar section:
   - Upload the CSV, plus a cookies file if every row needs the same authentication.
   - Choose whether to skip rows already marked as downloaded.
   - Optionally set a pause limit so you can review results in chunks.
4. Start the batch. Progress and running counts display in the main pane. When a pause limit is reached you can continue processing additional rows from the sidebar.
5. After each run you can download:
   - The updated CSV with status columns populated.
   - A ZIP archive containing successfully clipped files (when applicable).

## Troubleshooting
- **yt-dlp is outdated**: The app warns if the detected version is older than the recommended minimum; upgrade with `pip install --upgrade yt-dlp`.
- **Clipping options disabled**: Install a system `ffmpeg` build or ensure `imageio-ffmpeg` can download one.
- **HTTP 403 or login errors**: Refresh your browser cookies and upload a new cookies file before retrying.

## Development Notes
- Logging output is shown inside the app for both single and batch jobs; check it for detailed error messages.
- Adjust default output directory or other behavior by editing `video_downloader_app.py` and `video_downloader.py`.
