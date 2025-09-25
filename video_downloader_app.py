"""
Streamlit interface for the minimal video downloader.
"""
import logging
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional

import streamlit as st

from video_downloader import FFMPEG_AVAILABLE, FFMPEG_PATH, LOGGER, download_video

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%H:%M:%S"
DEFAULT_OUTPUT_DIR = Path("downloads")

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)

st.set_page_config(page_title="Video Downloader", page_icon=":inbox_tray:", layout="centered")
st.title("Video Downloader")
st.write(
    "Download a single video from most sites. For YouTube and other gated sources, upload cookies exported from your browser."
)

st.caption("Known issues: Does not work with some reigon-gated YouTube videos")

with st.form("download_form"):
    url = st.text_input("Video URL", placeholder="https://...")
    filename = st.text_input("Optional filename (without extension)")

    with st.expander("Cookies (required for YouTube/private content)"):
        st.markdown(
            """
            1. Install the [Get cookies.txt extension for Chrome/Edge](https://github.com/bugrammer/get_cookiestxt#chrome-extension) or the [Firefox add-on](https://github.com/bugrammer/get_cookiestxt#firefox-addon).
            2. Sign in to the site in that browser tab (e.g. youtube.com).
            3. Use the extension to export cookies for the current tab.
            4. Upload the exported .txt file here before downloading.
            """
        )
        cookies_file = st.file_uploader(
            "Cookies file (Netscape/yt-dlp format)",
            type=["txt", "json", "cookies"],
            help="Upload exported browser cookies to access private, age-gated, or logged-in content.",
        )

    submitted = st.form_submit_button("Download")

if submitted:
    if not url.strip():
        st.error("Please enter a video URL.")
    else:
        LOGGER.setLevel(logging.INFO)

        log_buffer = StringIO()
        handler = logging.StreamHandler(log_buffer)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))

        root_logger = logging.getLogger()
        previous_root_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

        yt_logger = logging.getLogger("yt_dlp")
        previous_yt_level = yt_logger.level
        yt_logger.setLevel(logging.INFO)

        output_dir = DEFAULT_OUTPUT_DIR
        result = None
        temp_cookie_path: Optional[Path] = None
        try:
            if cookies_file is not None:
                suffix = Path(cookies_file.name).suffix or ".txt"
                with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(cookies_file.getbuffer())
                    temp_cookie_path = Path(tmp.name)

            with st.spinner("Downloading video..."):
                result = download_video(
                    url.strip(),
                    output_dir,
                    filename.strip() or None,
                    temp_cookie_path,
                )
        finally:
            handler.flush()
            root_logger.removeHandler(handler)
            root_logger.setLevel(previous_root_level)
            yt_logger.setLevel(previous_yt_level)
            if temp_cookie_path and temp_cookie_path.exists():
                try:
                    temp_cookie_path.unlink()
                except OSError:
                    LOGGER.warning("Failed to remove temporary cookies file at %s", temp_cookie_path)

        log_output = log_buffer.getvalue().strip()
        if result:
            result_path = Path(result)
            st.success(f"Saved to {result_path}")
            st.caption(
                "The file path above is relative to where Streamlit is running. Files save under 'downloads/'."
            )

            file_bytes = result_path.read_bytes() if result_path.exists() else None
            if file_bytes:
                suffix = result_path.suffix.lower()
                mime = {
                    ".mp4": "video/mp4",
                    ".mkv": "video/x-matroska",
                    ".webm": "video/webm",
                    ".mov": "video/quicktime",
                }.get(suffix, "application/octet-stream")
                st.download_button(
                    "Download video",
                    data=file_bytes,
                    file_name=result_path.name,
                    mime=mime,
                )
            else:
                st.warning("Downloaded file could not be read for download.")
        else:
            st.error("Download failed. Check the logs for more details.")

        if log_output:
            st.text_area("Logs", log_output, height=240)
        else:
            st.caption("No log output captured for this run.")

st.divider()
st.write(
    "This downloader relies on yt-dlp, so any site supported by yt-dlp should work, "
    "provided the content is publicly accessible and not blocked by the host."
)


