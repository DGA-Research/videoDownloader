"""
Streamlit interface for the minimal video downloader.
"""
import logging
from io import StringIO
from pathlib import Path

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
        try:
            with st.spinner("Downloading video..."):
                result = download_video(url.strip(), output_dir, filename.strip() or None)
        finally:
            handler.flush()
            root_logger.removeHandler(handler)
            root_logger.setLevel(previous_root_level)
            yt_logger.setLevel(previous_yt_level)

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
