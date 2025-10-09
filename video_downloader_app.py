"""
Streamlit interface for the minimal video downloader.
"""
import csv
import logging
import zipfile
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import List, Optional, Tuple

import streamlit as st

from video_downloader import FFMPEG_AVAILABLE, FFMPEG_PATH, LOGGER, download_video, parse_time_to_seconds

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%H:%M:%S"
DEFAULT_OUTPUT_DIR = Path("downloads")

STATUS_COLUMN = "Download Status"
DETAIL_COLUMN = "Download Detail"
PATH_COLUMN = "Download Path"
TIMESTAMP_COLUMN = "Processed At"
COMPLETED_STATUS_VALUES = {"downloaded", "success"}

MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}



def _display_batch_results(data: dict) -> None:
    if not data:
        return

    paused_after = data.get("paused_after") or 0
    if paused_after:
        st.info(f"Batch processing paused after {paused_after} row(s) per user setting.")

    summary_counts = data.get("summary_counts") or st.session_state.get("batch_summary_counts") or {}
    success_count = summary_counts.get("success", data.get("success_count", 0))
    failure_count = summary_counts.get("failure", data.get("failure_count", 0))
    skipped_count = summary_counts.get("skipped", data.get("skipped_count", 0))

    if success_count:
        st.success(f"Downloaded {success_count} item(s) from the CSV.")
    if failure_count:
        st.error(f"{failure_count} download(s) failed. Check the logs for details.")
    if skipped_count:
        st.warning(f"Skipped {skipped_count} row(s) without a URL value.")

    results = data.get("results") or st.session_state.get("batch_all_results") or []
    if results:
        st.table(results)

    downloadable_items = data.get("downloadable_items") or st.session_state.get("batch_all_downloads") or []
    if downloadable_items:
        st.write("Download individual files:")
        for item in downloadable_items:
            saved_path = Path(item["path"])
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
                    key=f"csv_download_{row_index}",
                )
            else:
                st.warning(f"Downloaded file for row {row_index} not found at {saved_path}")

    remaining_rows = data.get("remaining_rows", 0)
    if remaining_rows:
        st.info(f"{remaining_rows} row(s) remain unprocessed in this batch.")
        default_limit = data.get("default_pause_limit") or remaining_rows
        default_limit = int(default_limit) if default_limit else remaining_rows
        default_limit = max(1, min(default_limit, remaining_rows))

        next_chunk = st.number_input(
            "Rows to process next",
            min_value=1,
            max_value=int(remaining_rows),
            value=int(default_limit),
            step=1,
            key="batch_continue_chunk_size",
        )
        skip_completed_default = data.get("skip_completed_default", True)
        next_skip_completed = st.checkbox(
            "Skip rows already marked as downloaded",
            value=bool(skip_completed_default),
            key="batch_continue_skip_completed",
        )
        if st.button("Continue batch", key="batch_continue_button"):
            st.session_state["continue_requested"] = True
            st.session_state["continue_chunk_size"] = int(next_chunk)
            st.session_state["continue_skip_completed"] = bool(next_skip_completed)

    log_output = data.get("log_output", "")
    if log_output:
        st.text_area("Batch logs", log_output, height=240, key="csv_batch_logs")

    updated_csv = data.get("updated_csv")
    if updated_csv:
        st.download_button(
            "Download updated CSV",
            data=updated_csv,
            file_name=data.get("updated_csv_filename") or "batch_results.csv",
            mime="text/csv",
            key="batch_updated_csv",
        )

    zip_bytes = data.get("zip_bytes")
    if zip_bytes:
        st.download_button(
            "Download all clips (.zip)",
            data=zip_bytes,
            file_name=data.get("zip_filename") or "clips.zip",
            mime="application/zip",
            key="batch_clips_zip",
        )


def _process_batch(context: dict, pause_limit: int, skip_completed: bool) -> Optional[dict]:
    rows = context.get("rows") or []
    total = len(rows)
    start_index = min(context.get("next_row", 0), total)
    context["next_row"] = start_index
    context["skip_completed_default"] = skip_completed
    context["last_pause_limit"] = pause_limit

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
    cookies_bytes = context.get("cookies_bytes")
    cookies_name = context.get("cookies_name") or "cookies.txt"
    try:
        if cookies_bytes:
            suffix = Path(cookies_name).suffix or ".txt"
            with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(cookies_bytes)
                temp_cookie_path = Path(tmp.name)

        progress = st.progress(start_index / total if total else 0.0) if total else None
        status_placeholder = st.empty()
        log_placeholder = st.empty()

        results = []
        downloadable_items = []

        summary_counts_state = st.session_state.get("batch_summary_counts") or {}
        downloaded_total = int(summary_counts_state.get("success", 0))
        failed_total = int(summary_counts_state.get("failure", 0))
        skipped_total = int(summary_counts_state.get("skipped", 0))
        pause_triggered = False
        processed_in_run = 0

        filename_candidates = context.get("filename_candidates") or (
            "File Name",
            "Filename",
            "file_name",
            "Name",
        )
        url_column = context["url_column"]
        skip_column = context.get("skip_column")
        status_column = context["status_column"]
        detail_column = context["detail_column"]
        path_column = context["path_column"]
        timestamp_column = context["timestamp_column"]

        def set_row_status(row_dict: dict, status: str, detail: str, path_value: Optional[Path] = None) -> None:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            row_dict[status_column] = status
            row_dict[detail_column] = detail
            row_dict[path_column] = str(path_value) if path_value else ""
            row_dict[timestamp_column] = timestamp

        def update_placeholders(row_number: int, status_text: str) -> None:
            if progress and total:
                progress.progress(min(1.0, row_number / total))
            status_placeholder.markdown(
                f"**Row {row_number}/{total}:** {status_text}\n\n"
                f"✅ Downloads: {downloaded_total} | ❌ Failures: {failed_total} | ⚪ Skipped: {skipped_total}"
            )
            log_lines = log_buffer.getvalue().strip().splitlines()
            if log_lines:
                recent = "\n".join(log_lines[-12:])
                log_placeholder.text(recent)
            else:
                log_placeholder.text("Logs will appear here while the batch runs.")

        if start_index >= total:
            status_placeholder.info("All rows in this batch have already been processed.")
        else:
            for zero_idx in range(start_index, total):
                row = rows[zero_idx]
                index = zero_idx + 1
                processed_in_run += 1

                if skip_completed:
                    existing_status = str(row.get(status_column, "")).strip().lower()
                    existing_path = row.get(path_column)
                    if existing_status in COMPLETED_STATUS_VALUES and existing_path:
                        detail_message = "Already marked as downloaded in CSV."
                        results.append(
                            {
                                "Row": index,
                                "URL": row.get(url_column, "").strip(),
                                "Status": "skipped",
                                "Detail": detail_message,
                            }
                        )
                        skipped_total += 1
                        update_placeholders(index, detail_message)
                        context["next_row"] = zero_idx + 1
                        if pause_limit and processed_in_run >= pause_limit:
                            pause_triggered = True
                            break
                        continue

                if skip_column:
                    skip_value = str(row.get(skip_column, "")).strip().lower()
                    if skip_value in {"1", "true", "yes", "skip"}:
                        detail_message = "Marked to skip via CSV."
                        results.append(
                            {
                                "Row": index,
                                "URL": row.get(url_column, "").strip(),
                                "Status": "skipped",
                                "Detail": detail_message,
                            }
                        )
                        set_row_status(row, "skipped", detail_message)
                        skipped_total += 1
                        context["next_row"] = zero_idx + 1
                        update_placeholders(index, detail_message)
                        if pause_limit and processed_in_run >= pause_limit:
                            pause_triggered = True
                            break
                        continue

                url_value = (row.get(url_column) or "").strip()
                if not url_value:
                    detail_message = "Missing URL value."
                    results.append({"Row": index, "URL": "", "Status": "skipped", "Detail": detail_message})
                    set_row_status(row, "skipped", detail_message)
                    skipped_total += 1
                    context["next_row"] = zero_idx + 1
                    update_placeholders(index, detail_message)
                    if pause_limit and processed_in_run >= pause_limit:
                        pause_triggered = True
                        break
                    continue

                filename_value = None
                for candidate in filename_candidates:
                    value = row.get(candidate)
                    if value and str(value).strip():
                        filename_value = str(value).strip()
                        break

                clip_start_raw = (row.get("Clip Start Time") or "").strip()
                clip_end_raw = (row.get("Clip End Time") or "").strip()
                clip_start_seconds: Optional[float] = None
                clip_end_seconds: Optional[float] = None
                row_errors = []

                if clip_start_raw:
                    clip_start_seconds = parse_time_to_seconds(clip_start_raw)
                    if clip_start_seconds is None:
                        row_errors.append("Invalid clip start time value.")
                if clip_end_raw:
                    clip_end_seconds = parse_time_to_seconds(clip_end_raw)
                    if clip_end_seconds is None:
                        row_errors.append("Invalid clip end time value.")
                if (
                    clip_start_seconds is not None
                    and clip_end_seconds is not None
                    and clip_end_seconds <= clip_start_seconds
                ):
                    row_errors.append("Clip end time must be greater than clip start time.")
                if (clip_start_seconds is not None or clip_end_seconds is not None) and not FFMPEG_AVAILABLE:
                    row_errors.append("ffmpeg not available for clipping.")

                if row_errors:
                    detail_message = "; ".join(row_errors)
                    results.append(
                        {
                            "Row": index,
                            "URL": url_value,
                            "Status": "failed",
                            "Detail": detail_message,
                        }
                    )
                    set_row_status(row, "failed", detail_message)
                    failed_total += 1
                    context["next_row"] = zero_idx + 1
                    update_placeholders(index, detail_message)
                    if pause_limit and processed_in_run >= pause_limit:
                        pause_triggered = True
                        break
                    continue

                saved_path = download_video(
                    url_value,
                    DEFAULT_OUTPUT_DIR,
                    filename_value,
                    temp_cookie_path,
                    clip_start=clip_start_seconds,
                    clip_end=clip_end_seconds,
                )
                if saved_path:
                    saved_path = Path(saved_path)
                    detail_message = str(saved_path)
                    results.append({"Row": index, "URL": url_value, "Status": "downloaded", "Detail": detail_message})
                    downloadable_items.append(
                        {
                            "row": index,
                            "path": str(saved_path),
                            "display_name": filename_value or saved_path.name,
                        }
                    )
                    set_row_status(row, "downloaded", detail_message, saved_path)
                    downloaded_total += 1
                else:
                    detail_message = "Download failed."
                    results.append({"Row": index, "URL": url_value, "Status": "failed", "Detail": detail_message})
                    set_row_status(row, "failed", detail_message)
                    failed_total += 1

                context["next_row"] = zero_idx + 1
                update_placeholders(index, detail_message)

                if pause_limit and processed_in_run >= pause_limit:
                    pause_triggered = True
                    update_placeholders(index, f"Paused automatically after {pause_limit} row(s).")
                    break

        if not pause_triggered:
            context["next_row"] = total

        if progress:
            progress.empty()
        if pause_triggered:
            status_placeholder.info(
                f"Batch paused after {pause_limit} row(s). Use the continue controls below to process more rows."
            )
            log_placeholder.text(log_buffer.getvalue().strip())
        else:
            status_placeholder.empty()
            log_placeholder.empty()
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

    fieldnames = context.get("fieldnames") or []
    status_column = context["status_column"]
    detail_column = context["detail_column"]
    path_column = context["path_column"]
    timestamp_column = context["timestamp_column"]

    updated_csv_buffer = StringIO()
    if fieldnames:
        writer = csv.DictWriter(updated_csv_buffer, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            for column_name in (status_column, detail_column, path_column, timestamp_column):
                row.setdefault(column_name, "")
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    updated_csv_bytes = updated_csv_buffer.getvalue().encode("utf-8-sig")

    successful_paths = []
    for row in rows:
        if str(row.get(status_column, "")).strip().lower() == "downloaded":
            path_str = row.get(path_column, "")
            if path_str:
                saved_path = Path(path_str)
                if saved_path.exists():
                    successful_paths.append(saved_path)

    zip_bytes = None
    zip_filename = context.get("source_filename") or "batch.csv"
    zip_filename = f"{Path(zip_filename).stem}_clips.zip"

    if successful_paths:
        buffer = BytesIO()
        used_names = set()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for saved_path in successful_paths:
                arcname = saved_path.name
                if arcname in used_names:
                    stem = saved_path.stem
                    suffix = saved_path.suffix
                    counter = 1
                    while True:
                        candidate = f"{stem}_{counter}{suffix}"
                        if candidate not in used_names:
                            arcname = candidate
                            break
                        counter += 1
                used_names.add(arcname)
                try:
                    zf.write(saved_path, arcname=arcname)
                except OSError as exc:
                    LOGGER.warning("Failed to add %s to archive: %s", saved_path, exc)
        buffer.seek(0)
        zip_bytes = buffer.getvalue()

    updated_csv_name = context.get("source_filename") or "batch.csv"
    updated_csv_name = f"{Path(updated_csv_name).stem}_with_status.csv"

    success_count = sum(1 for item in results if item["Status"] == "downloaded")
    failure_count = sum(1 for item in results if item["Status"] == "failed")
    skipped_count = sum(1 for item in results if item["Status"] == "skipped")
    batch_log_output = log_buffer.getvalue().strip()
    remaining_rows = max(0, total - context.get("next_row", total))

    batch_results = {
        "results": results,
        "downloadable_items": downloadable_items,
        "success_count": success_count,
        "failure_count": failure_count,
        "skipped_count": skipped_count,
        "log_output": batch_log_output,
        "paused_after": pause_limit if pause_triggered else 0,
        "updated_csv": updated_csv_bytes,
        "updated_csv_filename": updated_csv_name,
        "zip_bytes": zip_bytes,
        "zip_filename": zip_filename,
        "remaining_rows": remaining_rows,
        "default_pause_limit": pause_limit if pause_limit else context.get("last_pause_limit", 0),
        "skip_completed_default": skip_completed,
    }
    return batch_results


def _build_history_from_context(context: dict) -> Tuple[List[dict], List[dict], dict]:
    rows = context.get("rows") or []
    url_column = context.get("url_column")
    status_column = context.get("status_column")
    detail_column = context.get("detail_column")
    path_column = context.get("path_column")
    filename_candidates = context.get("filename_candidates") or ()

    processed_rows: List[dict] = []
    download_items: List[dict] = []
    summary_counts = {"success": 0, "failure": 0, "skipped": 0}

    for idx, row in enumerate(rows, start=1):
        status = str(row.get(status_column, "")).strip()
        if not status:
            continue
        status_lower = status.lower()
        if status_lower in {"downloaded", "success"}:
            summary_counts["success"] += 1
        elif status_lower == "failed":
            summary_counts["failure"] += 1
        elif status_lower == "skipped":
            summary_counts["skipped"] += 1

        entry = {
            "Row": idx,
            "URL": (row.get(url_column) or "").strip(),
            "Status": status,
            "Detail": row.get(detail_column, ""),
        }
        processed_rows.append(entry)

        if status_lower in {"downloaded", "success"}:
            path_str = row.get(path_column, "")
            if not path_str:
                continue
            try:
                saved_path = Path(path_str)
            except (TypeError, ValueError):
                continue
            display_name = ""
            for candidate in filename_candidates:
                value = row.get(candidate)
                if value and str(value).strip():
                    display_name = str(value).strip()
                    break
            download_items.append(
                {
                    "row": idx,
                    "path": path_str,
                    "display_name": display_name or saved_path.name,
                }
            )

    processed_rows.sort(key=lambda item: item["Row"])
    download_items.sort(key=lambda item: item["row"])
    return processed_rows, download_items, summary_counts


def _update_batch_history(context: dict, batch_results: dict) -> dict:
    processed_rows, download_items, summary_counts = _build_history_from_context(context)
    st.session_state["batch_all_results"] = processed_rows
    st.session_state["batch_all_downloads"] = download_items
    st.session_state["batch_summary_counts"] = summary_counts

    batch_results = dict(batch_results)
    batch_results["results"] = processed_rows
    batch_results["downloadable_items"] = download_items
    batch_results["summary_counts"] = summary_counts
    batch_results["success_count"] = summary_counts.get("success", 0)
    batch_results["failure_count"] = summary_counts.get("failure", 0)
    batch_results["skipped_count"] = summary_counts.get("skipped", 0)
    return batch_results

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)

st.set_page_config(page_title="Video Downloader", page_icon=":inbox_tray:", layout="centered")
st.title("Video Downloader")
st.write(
    "Download a single video from most sites. For YouTube and other gated sources, upload cookies exported from your browser."
)

st.caption("Known issues: Does not work with some reigon-gated YouTube videos")
if not FFMPEG_AVAILABLE:
    st.warning("ffmpeg not detected. Install ffmpeg to enable audio/video clipping and proper muxing.")

with st.form("download_form"):
    url = st.text_input("Video URL", placeholder="https://...")
    filename = st.text_input("Optional filename (without extension)")
    clip_start_input = st.text_input(
        "Clip start (optional)",
        placeholder="0:01:30",
        help="Accepts HH:MM:SS, MM:SS, or seconds. Requires ffmpeg.",
    )
    clip_end_input = st.text_input(
        "Clip end (optional)",
        placeholder="0:02:00",
        help="Accepts HH:MM:SS, MM:SS, or seconds. Requires ffmpeg.",
    )

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
        clip_start_raw = clip_start_input.strip()
        clip_end_raw = clip_end_input.strip()
        clip_start_seconds: Optional[float] = None
        clip_end_seconds: Optional[float] = None
        validation_errors = []

        if clip_start_raw:
            clip_start_seconds = parse_time_to_seconds(clip_start_raw)
            if clip_start_seconds is None:
                validation_errors.append("Clip start time must be in HH:MM:SS or seconds format.")
        if clip_end_raw:
            clip_end_seconds = parse_time_to_seconds(clip_end_raw)
            if clip_end_seconds is None:
                validation_errors.append("Clip end time must be in HH:MM:SS or seconds format.")
        if (
            clip_start_seconds is not None
            and clip_end_seconds is not None
            and clip_end_seconds <= clip_start_seconds
        ):
            validation_errors.append("Clip end time must be greater than clip start time.")
        if (clip_start_seconds is not None or clip_end_seconds is not None) and not FFMPEG_AVAILABLE:
            validation_errors.append("Clipping requires ffmpeg, which was not detected.")

        if validation_errors:
            for message in validation_errors:
                st.error(message)
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
                        clip_start=clip_start_seconds,
                        clip_end=clip_end_seconds,
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
                    mime = MIME_BY_SUFFIX.get(suffix, "application/octet-stream")
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
st.header("Batch Download from CSV")

batch_results = st.session_state.get("batch_results")
if batch_results:
    _display_batch_results(batch_results)

processing_triggered = False

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
    pause_after = st.number_input(
        "Process rows then pause (0 = run all)",
        min_value=0,
        value=0,
        step=1,
        help="Set to a positive number to stop after that many rows so you can download results before continuing.",
    )
    skip_completed = st.checkbox(
        "Skip rows already marked as downloaded in the CSV",
        value=True,
        help="When enabled, rows whose download status column already indicates success are not processed again.",
    )
    csv_submitted = st.form_submit_button("Download URLs from CSV")

if csv_submitted:
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
                fieldnames = reader.fieldnames
                if not fieldnames:
                    st.error("CSV file has no header row to identify columns.")
                else:
                    url_column = next(
                        (col for col in fieldnames if col and col.strip().lower() in {"url", "link", "links"}),
                        None,
                    )
                    if not url_column:
                        st.error("No column named URL, Link, or Links was found in the CSV header.")
                    else:
                        rows = list(reader)
                        if not rows:
                            st.warning("CSV did not contain any data rows.")
                        else:
                            def _find_column(name: str) -> Optional[str]:
                                lowered = name.lower()
                                for column in fieldnames:
                                    if column and column.strip().lower() == lowered:
                                        return column
                                return None

                            status_column = _find_column(STATUS_COLUMN) or STATUS_COLUMN
                            detail_column = _find_column(DETAIL_COLUMN) or DETAIL_COLUMN
                            path_column = _find_column(PATH_COLUMN) or PATH_COLUMN
                            timestamp_column = _find_column(TIMESTAMP_COLUMN) or TIMESTAMP_COLUMN

                            base_fieldnames = list(fieldnames)
                            for column_name in (status_column, detail_column, path_column, timestamp_column):
                                if column_name not in base_fieldnames:
                                    base_fieldnames.append(column_name)

                            skip_column = next(
                                (col for col in fieldnames if col and col.strip().lower() == "skip"),
                                None,
                            )

                            st.session_state["batch_all_results"] = []
                            st.session_state["batch_all_downloads"] = []
                            st.session_state["batch_summary_counts"] = {"success": 0, "failure": 0, "skipped": 0}

                            context = {
                                "rows": rows,
                                "fieldnames": base_fieldnames,
                                "url_column": url_column,
                                "skip_column": skip_column,
                                "status_column": status_column,
                                "detail_column": detail_column,
                                "path_column": path_column,
                                "timestamp_column": timestamp_column,
                                "filename_candidates": ("File Name", "Filename", "file_name", "Name"),
                                "next_row": 0,
                                "source_filename": getattr(csv_file, "name", "batch.csv"),
                                "cookies_bytes": batch_cookies_file.getvalue() if batch_cookies_file else None,
                                "cookies_name": getattr(batch_cookies_file, "name", None),
                            }

                            st.session_state["batch_context"] = context
                            batch_results = _process_batch(context, int(pause_after or 0), bool(skip_completed))
                            if batch_results is not None:
                                batch_results = _update_batch_history(context, batch_results)
                                st.session_state["batch_results"] = batch_results
                                processing_triggered = True
                                if batch_results.get("remaining_rows") == 0:
                                    st.session_state.pop("batch_context", None)

if st.session_state.pop("continue_requested", False):
    context = st.session_state.get("batch_context")
    if context:
        requested_pause = st.session_state.pop(
            "continue_chunk_size", context.get("last_pause_limit", 0) or 0
        )
        pause_limit = max(0, int(requested_pause))
        skip_choice = bool(
            st.session_state.pop("continue_skip_completed", context.get("skip_completed_default", True))
        )
        batch_results = _process_batch(context, pause_limit, skip_choice)
        if batch_results is not None:
            batch_results = _update_batch_history(context, batch_results)
            st.session_state["batch_results"] = batch_results
            processing_triggered = True
            if batch_results.get("remaining_rows") == 0:
                st.session_state.pop("batch_context", None)
    else:
        st.warning("No batch is currently loaded. Upload a CSV to start a new batch.")

if processing_triggered:
    st.rerun()

st.divider()
st.write(
    "This downloader relies on yt-dlp, so any site supported by yt-dlp should work, "
    "provided the content is publicly accessible and not blocked by the host."
)


