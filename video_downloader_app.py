"""
Streamlit interface for the minimal video downloader.
"""
import logging
from pathlib import Path

import streamlit as st

from video_downloader import FFMPEG_AVAILABLE, FFMPEG_PATH, download_video

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Video Downloader", page_icon=":inbox_tray:", layout="centered")
st.title("Video Downloader")
st.write("Download a single video with yt-dlp. Provide a link and choose where to save it.")

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
    submitted = st.form_submit_button("Download")

if submitted:
    if not url.strip():
        st.error("Please enter a video URL.")
    else:
        output_dir = Path(output_dir_text.strip() or "downloads")
        with st.spinner("Downloading video..."):
            result = download_video(url.strip(), output_dir, filename.strip() or None)
        if result:
            st.success(f"Saved to {result}")
            st.caption("The file path above is relative to where Streamlit is running.")
        else:
            st.error("Download failed. Check the logs for more details.")

st.divider()
st.write(
    "This downloader relies on yt-dlp, so any site supported by yt-dlp should work, "
    "provided the content is publicly accessible and not blocked by the host."
)
