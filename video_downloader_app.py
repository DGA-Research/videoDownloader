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
import html

from video_downloader import (
    FFMPEG_AVAILABLE,
    FFMPEG_PATH,
    LOGGER,
    download_video,
    parse_time_to_seconds,
    yt_dlp_version_status,
)

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



def _display_batch_results(data: dict, controls_container=None) -> None:
    if not data:
        return

    summary_counts = data.get("summary_counts") or st.session_state.get("batch_summary_counts") or {}
    success_count = summary_counts.get("success", data.get("success_count", 0))
    failure_count = summary_counts.get("failure", data.get("failure_count", 0))
    skipped_count = summary_counts.get("skipped", data.get("skipped_count", 0))

    status_slot = st.session_state.get("batch_status_placeholder")
    if status_slot is None:
        status_slot = st.empty()
        st.session_state["batch_status_placeholder"] = status_slot
    progress_slot = st.session_state.get("batch_progress_placeholder")
    if progress_slot is None:
        progress_slot = st.empty()
        st.session_state["batch_progress_placeholder"] = progress_slot

    live_active = st.session_state.get("batch_live_active", False)
    live_row_text = st.session_state.get("batch_live_row_text")
    live_counts_text = st.session_state.get("batch_live_counts_text")

    if live_active:
        if live_row_text or live_counts_text:
            combined = "\n\n".join(filter(None, [live_row_text, live_counts_text]))
            status_slot.markdown(combined)
        else:
            status_slot.empty()
    else:
        status_slot.markdown(
            f"✅ Downloads: {success_count} | ❌ Failures: {failure_count} | ⚪ Skipped: {skipped_count}"
        )
        if progress_slot:
            progress_slot.empty()

    results = data.get("results") or st.session_state.get("batch_all_results") or []
    downloadable_items = data.get("downloadable_items") or st.session_state.get("batch_all_downloads") or []
    download_map = {item["row"]: item for item in downloadable_items or []}

    if results:
        st.markdown(
            """
            <style>
            .batch-results-scroll {
                max-height: 360px;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
                gap: 0.25rem;
            }
            .batch-results-scroll > div[data-testid="stHorizontalBlock"] {
                margin-bottom: 0 !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        table_container = st.container()
        with table_container:
            st.markdown('<div class="batch-results-scroll">', unsafe_allow_html=True)
            header_cols = st.columns([1, 3, 2, 4, 2])
            header_cols[0].markdown("**Row**")
            header_cols[1].markdown("**URL**")
            header_cols[2].markdown("**Status**")
            header_cols[3].markdown("**Detail**")
            header_cols[4].markdown("**Download**")

            for entry in results:
                row_number = entry.get("Row", "")
                cols = st.columns([1, 3, 2, 4, 2])
                cols[0].write(row_number)
                url_value = entry.get("URL", "")
                status_value = entry.get("Status", "")
                detail_value = entry.get("Detail", "")
                cols[1].markdown(
                    f"<div style='word-break: break-word;'>{html.escape(url_value or '')}</div>",
                    unsafe_allow_html=True,
                )
                cols[2].write(status_value)
                cols[3].markdown(
                    f"<div style='word-break: break-word;'>{html.escape(detail_value or '')}</div>",
                    unsafe_allow_html=True,
                )

                download_cell = cols[4]
                status_lower = str(entry.get("Status", "")).strip().lower()
                if status_lower in COMPLETED_STATUS_VALUES and row_number in download_map:
                    download_info = download_map[row_number]
                    saved_path = Path(download_info.get("path", ""))
                    display_name = download_info.get("display_name") or saved_path.name
                    if saved_path.exists():
                        try:
                            file_bytes = saved_path.read_bytes()
                            suffix = saved_path.suffix.lower()
                            mime = MIME_BY_SUFFIX.get(suffix, "application/octet-stream")
                            download_cell.download_button(
                                "Download",
                                data=file_bytes,
                                file_name=saved_path.name,
                                mime=mime,
                                key=f"table_download_{row_number}",
                            )
                        except OSError as exc:
                            download_cell.write(f"Unavailable ({exc})")
                    else:
                        download_cell.write("File missing")
                else:
                    download_cell.write("—")

            st.markdown("</div>", unsafe_allow_html=True)

    paused_after = data.get("paused_after") or 0
    if paused_after:
        st.info(f"Batch processing paused after {paused_after} row(s) per user setting.")
    if success_count:
        st.success(f"Downloaded {success_count} item(s) from the CSV.")
    if failure_count:
        st.error(f"{failure_count} download(s) failed. Check the logs for details.")
    if skipped_count:
        st.warning(f"Skipped {skipped_count} row(s) without a URL value.")

    remaining_rows = data.get("remaining_rows", 0)
    if remaining_rows:
        st.info(
            f"{remaining_rows} row(s) remain unprocessed in this batch. Use the sidebar controls to continue processing."
        )
        default_limit = data.get("default_pause_limit") or remaining_rows
        default_limit = int(default_limit) if default_limit else remaining_rows
        default_limit = max(1, min(default_limit, remaining_rows))

        skip_completed_default = data.get("skip_completed_default", True)
        container_target = controls_container or st.sidebar
        continue_controls = container_target.container()
        continue_controls.subheader("Continue Batch")
        next_chunk = continue_controls.number_input(
            "Rows to process next",
            min_value=1,
            max_value=int(remaining_rows),
            value=int(default_limit),
            step=1,
            key="batch_continue_chunk_size",
        )
        next_skip_completed = bool(st.session_state.get("batch_skip_completed_toggle", skip_completed_default))
        if continue_controls.button("Continue batch", key="batch_continue_button"):
            st.session_state["batch_live_active"] = True
            st.session_state["batch_live_row_text"] = None
            st.session_state["batch_live_counts_text"] = None
            st.session_state["continue_requested"] = True
            st.session_state["continue_chunk_size"] = int(next_chunk)
            st.session_state["continue_skip_completed"] = bool(next_skip_completed)

    log_output = data.get("log_output", "")
    if log_output:
        st.text_area("Batch logs", log_output, height=240, key="csv_batch_logs")
        if "HTTP Error 403: Forbidden" in log_output and not st.session_state.get("cookie_refresh_prompt", False):
            st.session_state["cookie_refresh_prompt"] = True

    updated_csv = data.get("updated_csv")
    zip_bytes = data.get("zip_bytes")
    if updated_csv and controls_container:
        controls_container.download_button(
            "Download updated CSV",
            data=updated_csv,
            file_name=data.get("updated_csv_filename") or "batch_results.csv",
            mime="text/csv",
            key="batch_updated_csv",
        )
    if zip_bytes and controls_container:
        controls_container.download_button(
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

        progress_slot = st.session_state.get("batch_progress_placeholder")
        if progress_slot is None:
            progress_slot = st.empty()
            st.session_state["batch_progress_placeholder"] = progress_slot
        progress = progress_slot.progress(start_index / total if total else 0.0) if total else None

        status_placeholder = st.session_state.get("batch_status_placeholder")
        if status_placeholder is None:
            status_placeholder = st.empty()
            st.session_state["batch_status_placeholder"] = status_placeholder

        st.session_state["batch_live_active"] = True
        st.session_state["batch_live_row_text"] = None
        st.session_state["batch_live_counts_text"] = None
        status_placeholder.empty()

        log_placeholder = None

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
            row_line = f"Row {row_number}/{total}: {status_text}"
            counts_line = (
                f"✅ Downloads: {downloaded_total} | ❌ Failures: {failed_total} | ⚪ Skipped: {skipped_total}"
            )
            status_placeholder.markdown(f"{row_line}\n\n{counts_line}")
            st.session_state["batch_live_row_text"] = row_line
            st.session_state["batch_live_counts_text"] = counts_line
            if log_placeholder:
                log_lines = log_buffer.getvalue().strip().splitlines()
                if log_lines:
                    recent = "\n".join(log_lines[-12:])
                    log_placeholder.text(recent)
                else:
                    log_placeholder.text("Logs will appear here while the batch runs.")

        if start_index >= total:
            st.session_state["batch_live_active"] = False
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
                        set_row_status(row, "skipped", detail_message)
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

        if progress_slot:
            progress_slot.empty()
        st.session_state["batch_live_active"] = False
        st.session_state["batch_live_row_text"] = None
        st.session_state["batch_live_counts_text"] = None
        if pause_triggered:
            status_placeholder.info(
                f"Batch paused after {pause_limit} row(s). Use the sidebar continue controls to process more rows."
            )
        else:
            status_placeholder.empty()
    finally:
        st.session_state["batch_live_active"] = False
        st.session_state["batch_live_row_text"] = None
        st.session_state["batch_live_counts_text"] = None
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
    "Downloads video from most sites. For YouTube and other gated sources, upload cookies exported from your browser."
)
current_yt_dlp, minimum_yt_dlp, yt_dlp_outdated = yt_dlp_version_status()
if yt_dlp_outdated:
    displayed_version = current_yt_dlp or "unknown"
    st.warning(
        f"yt-dlp {displayed_version} detected. Upgrade to {minimum_yt_dlp} or newer with "
        "`pip install --upgrade yt-dlp` to avoid recent YouTube download restrictions."
    )

st.caption("Known issues: Does not work with some reigon-gated YouTube videos")
if not FFMPEG_AVAILABLE:
    st.warning("ffmpeg not detected. Install ffmpeg to enable audio/video clipping and proper muxing.")

if "batch_progress_placeholder" not in st.session_state:
    st.session_state["batch_progress_placeholder"] = st.empty()
if "batch_status_placeholder" not in st.session_state:
    st.session_state["batch_status_placeholder"] = st.empty()
if "batch_live_active" not in st.session_state:
    st.session_state["batch_live_active"] = False
if "batch_live_row_text" not in st.session_state:
    st.session_state["batch_live_row_text"] = None
if "batch_live_counts_text" not in st.session_state:
    st.session_state["batch_live_counts_text"] = None
if "cookie_refresh_prompt" not in st.session_state:
    st.session_state["cookie_refresh_prompt"] = False

single_download_output = st.container()
single_download_expander = st.sidebar.expander("Single Video Download", expanded=True)
with single_download_expander.form("download_form"):
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

    cookies_file = st.file_uploader(
        "Cookies file (Netscape/yt-dlp format)",
        type=["txt", "json", "cookies"],
        help="Upload exported browser cookies to access private, age-gated, or logged-in content.",
    )
    st.caption(
        "Need cookies? Install the Get cookies.txt extension, export cookies from your signed-in browser tab, "
        "and upload the file here before downloading."
    )

    submitted = st.form_submit_button("Download", use_container_width=True)

if submitted:
    with single_download_output:
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
st.sidebar.markdown("---")
batch_download_expander = st.sidebar.expander("Batch Downloads", expanded=True)
with batch_download_expander:
    st.caption("Upload your batch CSV and optional cookies used for authenticated downloads.")
    batch_context = st.session_state.get("batch_context")
    batch_locked = bool(batch_context and batch_context.get("next_row", 0) > 0)
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
    st.caption(
        "Need cookies? Install the Get cookies.txt extension, export cookies from your signed-in browser tab, "
        "and upload the file here before downloading."
    )
    if st.session_state.get("cookie_refresh_prompt"):
        st.warning(
            "Recent downloads hit HTTP 403 errors. Upload fresh cookies before continuing.",
            icon="⚠️",
        )
    if "batch_pause_after" not in st.session_state:
        st.session_state["batch_pause_after"] = 0
    if batch_locked:
        pause_after_sidebar = int(st.session_state.get("batch_pause_after", 0))
    else:
        pause_after_sidebar = st.number_input(
            "Process rows then pause (0 = run all)",
            min_value=0,
            value=int(st.session_state.get("batch_pause_after", 0)),
            step=1,
            help="Set to a positive number to stop after that many rows so you can download results before continuing.",
            key="batch_pause_after",
        )
    skip_completed = st.checkbox(
        "Skip rows already marked as downloaded in the CSV",
        value=True,
        help="When enabled, rows whose download status column already indicates success are not processed again.",
        key="batch_skip_completed_toggle",
    )

    if batch_locked:
        csv_submitted = False
    else:
        csv_submitted = st.button("Download URLs from CSV", use_container_width=True)
        if csv_submitted:
            st.session_state["batch_live_active"] = True
            st.session_state["batch_live_row_text"] = None
            st.session_state["batch_live_counts_text"] = None


batch_results = st.session_state.get("batch_results")
if batch_results:
    _display_batch_results(batch_results, batch_download_expander)

processing_triggered = False
pause_after = int(pause_after_sidebar)

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

