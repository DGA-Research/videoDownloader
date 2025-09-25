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
LOG_LEVELS = {
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)

st.set_page_config(page_title="Video Downloader", page_icon=":inbox_tray:", layout="centered")
st.title("Video Downloader")
st.write("Download a single video from any yt-dlp-supported site. Provide a link and choose where to save it.")

if FFMPEG_AVAILABLE and FFMPEG_PATH:
    st.caption(f"ffmpeg available at {FFMPEG_PATH}")
elif not FFMPEG_AVAILABLE:
    st.info(
        "ffmpeg is not available on this system. Downloads will fall back to the best single file. "
        "Install ffmpeg to enable merging separate video and audio streams."
    )

with st.form("download_form"):
    url = st.text_input("Video URL", placeholder="https://...")
    output_dir_text = st.text_input("Output Directory", value="downloads")
    filename = st.text_input("Optional filename (without extension)")
    log_level_choice = st.selectbox("Log level", options=list(LOG_LEVELS.keys()), index=2)

    with st.expander("Authentication options"):
        cookies_file = st.file_uploader(
            "Cookies file (Netscape/yt-dlp format)",
            type=["txt", "json", "cookies"],
            help="Upload exported browser cookies to access private or age-gated content.",
        )
        username = st.text_input("Username", placeholder="Only if the site requires it")
        password = st.text_input("Password", type="password")

    submitted = st.form_submit_button("Download")

if submitted:
    if not url.strip():
        st.error("Please enter a video URL.")
    else:
        log_level = LOG_LEVELS[log_level_choice]
        LOGGER.setLevel(log_level)

        log_buffer = StringIO()
        handler = logging.StreamHandler(log_buffer)
        handler.setLevel(log_level)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))

        root_logger = logging.getLogger()
        previous_root_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(log_level)

        yt_logger = logging.getLogger("yt_dlp")
        previous_yt_level = yt_logger.level
        yt_logger.setLevel(log_level)

        output_dir = Path(output_dir_text.strip() or "downloads")
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
                    username.strip() or None,
                    password or None,
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
            st.caption("The file path above is relative to where Streamlit is running.")

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
