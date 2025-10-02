"""
Streamlit interface for the minimal video downloader.
"""
import csv
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

MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


# -------------------------
# Helpers (UI rendering)
# -------------------------

def _display_single_result(data: dict) -> None:
    """Render the single-download panel from a stored result dict in session_state."""
    if not data:
        return

    saved_path_str = data.get("path")
    log_output = data.get("log_output", "")

    if saved_path_str:
        saved_path = Path(saved_path_str)
        st.success(f"Saved to {saved_path}")
        st.caption(
            "The file path above is relative to where Streamlit is running. Files save under 'downloads/'."
        )
        if saved_path.exists():
            try:
                file_bytes = saved_path.read_bytes()
                suffix = saved_path.suffix.lower()
                mime = MIME_BY_SUFFIX.get(suffix, "application/octet-stream")
                st.download_button(
                    "Download video",
                    data=file_bytes,
                    file_name=saved_path.name,
                    mime=mime,
                    key=f"single_download_{saved_path.name}",
                )
            except OSError as exc:
                st.warning(f"Downloaded file could not be read for download: {exc}")
        else:
            st.warning("Downloaded file was not found on disk.")

    if log_output:
        st.text_area("Logs", log_output, height=240, key="single_logs")


def _display_batch_results(data: dict) -> None:
    if not data:
        return

    success_count = data.get("success_count", 0)
    failure_count = data.get("failure_count", 0)
    skipped_count = data.get("skipped_count", 0)

    if success_count:
        st.success(f"Downloaded {success_count} item(s) from the CSV.")
    if failure_count:
        st.error(f"{failure_count} download(s) failed. Check the logs for details.")
    if skipped_count:
        st.warning(f"Skipped {skipped_count} row(s) without a URL value.")

    results = data.get("results") or []
    if results:
        st.table(results)

    # IMPORTANT: Render per-row download buttons from stored state so they persist across reruns
    downloadable_items = data.get("downloadable_items") or []
    if downloadable_items:
        st.write("Download individual files:")
        for item in downloadable_items:
            saved_path = Path(item["path"]).resolve()
            row_index = item["row"]
            display_name = item["display_name"]
            if saved_path.exists():
                try:
                    file_bytes = saved_path.read_bytes()
                except OSError as exc:
                    st.warning(f"Could not read downloaded file for row {row_index}: {exc}")
                    continue
                suffix = saved_path.suffix.lower()
                mime = MIME_BY_SUFFIX.get(suffix, "application/octet-stream")
                # Use a stable, unique key so buttons don't collapse other buttons on click
                st.download_button(
                    f"Download row {row_index}: {display_name}",
                    data=file_bytes,
                    file_name=saved_path.name,
                    mime=mime,
                    key=f"csv_download_{row_index}_{saved_path.name}",
                )
            else:
                st.warning(f"Downloaded file for row {row_index} not found at {saved_path}")

    log_output = data.get("log_output", "")
    if log_output:
        st.text_area("Batch logs", log_output, height=240, key="csv_batch_logs")


# -------------------------
# Page config / Header
# -------------------------
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)

st.set_page_config(page_title="Video Downloader", page_icon=":inbox_tray:", layout="centered")
st.title("Video Downloader")
st.write(
    "Download a single video from most sites. For YouTube and other gated sources, upload cookies exported from your browser."
)

st.caption("Known issues: Does not work with some reigon-gated YouTube videos")


# -------------------------
# Single Video Section
# -------------------------
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
        # Persist single result to session_state so the download button survives reruns
        st.session_state["single_result"] = {
            "path": str(result) if result else None,
            "log_output": log_output,
        }

# Always render the last single download result (if any), so the button persists after clicks/reruns
if st.session_state.get("single_result"):
    _display_single_result(st.session_state["single_result"])


# -------------------------
# Batch Section
# -------------------------
st.divider()
st.header("Batch Download from CSV")

with st.form("csv_download_form"):
    csv_file = st.file_uploader(
        "CSV file with URLs",
        type=["csv"],
        help="Upload a CSV containing a column named URL, Link, or Links.",
    )
    batch_cookies_file = st.file_uploader(
        "Cookies file for all rows (optional)",
        type=["txt", "json", "cookies"],
        help="Upload cookies to use for every URL in the CSV batch.",
        key="csv_cookies",
    )
    csv_submitted = st.form_submit_button("Download URLs from CSV")

if csv_submitted:
    # Clear previous batch results only when a NEW batch is submitted
    st.session_state.pop("batch_results", None)
    if not csv_file:
        st.error("Please upload a CSV file.")
    else:
        csv_bytes = csv_file.getvalue()
        if not csv_bytes:
            st.error("Uploaded CSV file is empty.")
        else:
            decoded = None
            for encoding in ("utf-8-sig", "utf-8", "cp1252"):
                try:
                    decoded = csv_bytes.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if decoded is None:
                st.error("Could not decode CSV file. Please upload UTF-8 encoded CSVs.")
            else:
                reader = csv.DictReader(StringIO(decoded))
                if not reader.fieldnames:
                    st.error("CSV file has no header row to identify columns.")
                else:
                    url_column = next(
                        (col for col in reader.fieldnames if col and col.strip().lower() in {"url", "link", "links"}),
                        None,
                    )
                    if not url_column:
                        st.error("No column named URL, Link, or Links was found in the CSV header.")
                    else:
                        rows = list(reader)
                        if not rows:
                            st.warning("CSV did not contain any data rows.")
                        else:
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

                            temp_cookie_path: Optional[Path] = None
                            try:
                                if batch_cookies_file is not None:
                                    suffix = Path(batch_cookies_file.name).suffix or ".txt"
                                    with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                                        tmp.write(batch_cookies_file.getbuffer())
                                        temp_cookie_path = Path(tmp.name)

                                progress = st.progress(0)
                                status_placeholder = st.empty()
                                results = []
                                downloadable_items = []
                                total = len(rows)
                                filename_candidates = ("File Name", "Filename", "file_name", "Name")

                                for index, row in enumerate(rows, start=1):
                                    url_value = (row.get(url_column) or "").strip()
                                    if not url_value:
                                        LOGGER.warning("Row %s has an empty URL; skipping.", index)
                                        results.append(
                                            {"Row": index, "URL": "", "Status": "skipped", "Detail": "Missing URL value."}
                                        )
                                        progress.progress(index / total)
                                        status_placeholder.write(f"Processed {index}/{total}")
                                        continue

                                    filename_value = None
                                    for candidate in filename_candidates:
                                        value = row.get(candidate)
                                        if value and value.strip():
                                            filename_value = value.strip()
                                            break

                                    saved_path = download_video(
                                        url_value,
                                        DEFAULT_OUTPUT_DIR,
                                        filename_value,
                                        temp_cookie_path,
                                    )
                                    if saved_path:
                                        saved_path = Path(saved_path).resolve()
                                        results.append(
                                            {"Row": index, "URL": url_value, "Status": "downloaded", "Detail": str(saved_path)}
                                        )
                                        downloadable_items.append(
                                            {
                                                "row": index,
                                                "path": str(saved_path),
                                                "display_name": filename_value or Path(saved_path).name,
                                            }
                                        )
                                    else:
                                        results.append(
                                            {"Row": index, "URL": url_value, "Status": "failed", "Detail": "Download failed."}
                                        )
                                    progress.progress(index / total)
                                    status_placeholder.write(f"Processed {index}/{total}")

                                progress.empty()
                                status_placeholder.empty()
                            finally:
                                handler.flush()
                                root_logger.removeHandler(handler)
                                root_logger.setLevel(previous_root_level)
                                yt_logger.setLevel(previous_yt_level)
                                if temp_cookie_path and temp_cookie_path.exists():
                                    try:
                                        temp_cookie_path.unlink()
                                    except OSError:
                                        LOGGER.warning(
                                            "Failed to remove temporary cookies file at %s", temp_cookie_path
                                        )

                            success_count = sum(1 for item in results if item["Status"] == "downloaded")
                            failure_count = sum(1 for item in results if item["Status"] == "failed")
                            skipped_count = sum(1 for item in results if item["Status"] == "skipped")
                            batch_log_output = log_buffer.getvalue().strip()
                            st.session_state["batch_results"] = {
                                "results": results,
                                "downloadable_items": downloadable_items,
                                "success_count": success_count,
                                "failure_count": failure_count,
                                "skipped_count": skipped_count,
                                "log_output": batch_log_output,
                            }

# Always render the last batch results (if any), so all row download buttons persist after clicks/reruns
if st.session_state.get("batch_results"):
    st.divider()
    st.subheader("Batch results")
    _display_batch_results(st.session_state["batch_results"])


st.divider()
st.write(
    "This downloader relies on yt-dlp, so any site supported by yt-dlp should work, "
    "provided the content is publicly accessible and not blocked by the host."
)
