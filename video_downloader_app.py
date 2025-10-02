"""
Streamlit interface for the minimal video downloader (+ optional clipping).
"""
import csv
import logging
import subprocess
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional, Tuple

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
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
}


# -------------------------
# Clipping helpers
# -------------------------

def _parse_timecode(value: Optional[str]) -> Optional[str]:
    """Accepts H:MM:SS(.ms), MM:SS(.ms), SS(.ms) and returns ffmpeg-friendly string or None."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Normalize common variants like "1:2" -> "00:01:02"
    parts = s.split(":")
    try:
        if len(parts) == 1:
            # seconds or seconds.ms
            sec = float(parts[0])
            return f"{sec:.3f}".rstrip("0").rstrip(".")
        elif len(parts) == 2:
            m = int(parts[0])
            sec = float(parts[1])
            total = m * 60 + sec
            return f"{total:.3f}".rstrip("0").rstrip(".")
        elif len(parts) == 3:
            h = int(parts[0])
            m = int(parts[1])
            sec = float(parts[2])
            total = h * 3600 + m * 60 + sec
            return f"{total:.3f}".rstrip("0").rstrip(".")
    except ValueError:
        return None
    return None


def _clip_with_ffmpeg(
    input_path: Path,
    output_path: Path,
    start: Optional[str],
    end: Optional[str],
    audio_only: bool = False,
) -> bool:
    """Invoke ffmpeg to trim media between start and end. Returns True on success.

    Uses stream copy when possible for speed. Falls back to re-encode for MP3 extraction when audio_only is True.
    """
    if not FFMPEG_AVAILABLE or not FFMPEG_PATH:
        LOGGER.error("ffmpeg is not available; cannot clip media.")
        return False

    cmd = [str(FFMPEG_PATH), "-y"]

    # Fast (keyframe) seek: place -ss before -i. If precise frame accuracy is needed, move -ss after -i.
    if start:
        cmd += ["-ss", start]
    cmd += ["-i", str(input_path)]
    if end:
        cmd += ["-to", end]

    if audio_only:
        # Re-encode to MP3 for broad compatibility
        cmd += ["-vn", "-acodec", "libmp3lame", "-q:a", "2"]
    else:
        # Try stream copy to avoid re-encoding
        cmd += ["-c", "copy"]

    cmd += [str(output_path)]

    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return True
        # If stream copy failed (common when not cutting on keyframes), try a safe re-encode for video
        if not audio_only:
            reencode = [
                str(FFMPEG_PATH), "-y",
                *( ["-ss", start] if start else [] ),
                "-i", str(input_path),
                *( ["-to", end] if end else [] ),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-c:a", "aac", "-movflags", "+faststart",
                str(output_path),
            ]
            result2 = subprocess.run(reencode, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            return result2.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0
        return False
    except Exception as exc:
        LOGGER.exception("ffmpeg clipping failed: %s", exc)
        return False


def _derive_clip_paths(base: Path, prefer_audio: bool, explicit_name: Optional[str]) -> Tuple[Path, bool]:
    """Create an output path for the clip next to the downloaded file.
    Returns (path, audio_only?).
    """
    if prefer_audio:
        name = (explicit_name or base.stem) + ".mp3"
        return base.with_name(name), True
    # default: keep container as mp4 unless base has other suffix
    suffix = base.suffix if base.suffix else ".mp4"
    name = (explicit_name or base.stem) + suffix
    return base.with_name(name), False


# -------------------------
# UI Helpers
# -------------------------

def _display_single_result(data: dict) -> None:
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
    "Download a single video or batch from CSV. Optionally clip to start/end times and export audio-only."
)

st.caption("Known issues: Some region-gated sources may require valid cookies.")


# -------------------------
# Single Video Section
# -------------------------
with st.form("download_form"):
    url = st.text_input("Video URL", placeholder="https://...")
    filename = st.text_input("Optional output name (without extension)")

    col1, col2, col3 = st.columns(3)
    with col1:
        start_tc = st.text_input("Clip start (H:MM:SS or seconds)", placeholder="", help="Leave blank to start from 0")
    with col2:
        end_tc = st.text_input("Clip end (H:MM:SS or seconds)", placeholder="", help="Leave blank to keep till end")
    with col3:
        audio_only_single = st.checkbox("Export audio-only (MP3)", value=False)

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

            # Optional clipping
            parsed_start = _parse_timecode(start_tc)
            parsed_end = _parse_timecode(end_tc)
            final_path = Path(result) if result else None
            if result and (parsed_start or parsed_end or audio_only_single):
                base_path = Path(result)
                clip_path, audio_only_flag = _derive_clip_paths(
                    base_path, prefer_audio=audio_only_single, explicit_name=(filename.strip() if filename else None)
                )
                with st.spinner("Clipping..."):
                    ok = _clip_with_ffmpeg(base_path, clip_path, parsed_start, parsed_end, audio_only=audio_only_flag)
                if ok:
                    final_path = clip_path
                else:
                    st.warning("Clipping failed; returning original download.")
                    final_path = base_path
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
        st.session_state["single_result"] = {
            "path": str(final_path) if result else None,
            "log_output": log_output,
        }

# Always render the last single download result (if any)
if st.session_state.get("single_result"):
    _display_single_result(st.session_state["single_result"])


# -------------------------
# Batch Section (CSV)
# -------------------------
# Expected headers (case-insensitive): URL, Download Type (audio/video) [optional], File Name [optional], Clip Start Time, Clip End Time
st.divider()
st.header("Batch Download from CSV")

with st.form("csv_download_form"):
    csv_file = st.file_uploader(
        "CSV file with URLs (columns: URL, File Name, Clip Start Time, Clip End Time, Download Type)",
        type=["csv"],
        help="Upload a CSV containing at least a URL column. Optional: File Name, Clip Start Time, Clip End Time, Download Type (audio/video).",
    )
    batch_cookies_file = st.file_uploader(
        "Cookies file for all rows (optional)",
        type=["txt", "json", "cookies"],
        help="Upload cookies to use for every URL in the CSV batch.",
        key="csv_cookies",
    )
    csv_submitted = st.form_submit_button("Download URLs from CSV")

if csv_submitted:
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
                    # Normalize header names for flexible matching
                    def _find(colset):
                        return next((c for c in reader.fieldnames if c and c.strip().lower() in colset), None)

                    url_column = _find({"url", "link", "links"})
                    file_name_column = _find({"file name", "filename", "file_name", "name"})
                    start_column = _find({"clip start time", "start", "start time", "clip start"})
                    end_column = _find({"clip end time", "end", "end time", "clip end"})
                    type_column = _find({"download type", "type", "media type"})

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

                                for index, row in enumerate(rows, start=1):
                                    url_value = (row.get(url_column) or "").strip()
                                    if not url_value:
                                        LOGGER.warning("Row %s has an empty URL; skipping.", index)
                                        results.append({"Row": index, "URL": "", "Status": "skipped", "Detail": "Missing URL value."})
                                        progress.progress(index / total)
                                        status_placeholder.write(f"Processed {index}/{total}")
                                        continue

                                    filename_value = (row.get(file_name_column) or "").strip() if file_name_column else None
                                    start_value = _parse_timecode((row.get(start_column) or "").strip() if start_column else None)
                                    end_value = _parse_timecode((row.get(end_column) or "").strip() if end_column else None)
                                    media_type = (row.get(type_column) or "").strip().lower() if type_column else ""
                                    prefer_audio = media_type == "audio"

                                    saved_path = download_video(
                                        url_value,
                                        DEFAULT_OUTPUT_DIR,
                                        filename_value or None,
                                        temp_cookie_path,
                                    )

                                    if saved_path:
                                        base_path = Path(saved_path).resolve()
                                        final_saved = base_path

                                        if start_value or end_value or prefer_audio:
                                            clip_path, audio_only_flag = _derive_clip_paths(base_path, prefer_audio, filename_value or None)
                                            ok = _clip_with_ffmpeg(base_path, clip_path, start_value, end_value, audio_only=audio_only_flag)
                                            if ok:
                                                final_saved = clip_path
                                            else:
                                                LOGGER.warning("Clipping failed for row %s; keeping original.", index)

                                        results.append({
                                            "Row": index,
                                            "URL": url_value,
                                            "Status": "downloaded",
                                            "Detail": str(final_saved),
                                            "Start": start_value or "",
                                            "End": end_value or "",
                                            "Type": ("audio" if (prefer_audio or final_saved.suffix.lower() in {".mp3", ".m4a"}) else "video"),
                                        })
                                        downloadable_items.append({
                                            "row": index,
                                            "path": str(final_saved),
                                            "display_name": filename_value or final_saved.name,
                                        })
                                    else:
                                        results.append({"Row": index, "URL": url_value, "Status": "failed", "Detail": "Download failed."})

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
                                        LOGGER.warning("Failed to remove temporary cookies file at %s", temp_cookie_path)

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

# Always render the last batch results (if any)
if st.session_state.get("batch_results"):
    st.divider()
    st.subheader("Batch results")
    _display_batch_results(st.session_state["batch_results"])


st.divider()
st.write(
    "This downloader relies on yt-dlp and ffmpeg. For clipping, provide start/end timecodes as H:MM:SS or seconds."
)
