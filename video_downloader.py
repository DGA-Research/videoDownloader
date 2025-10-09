"""
Minimal video downloader for sites supported by yt-dlp.
"""
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


try:
    import yt_dlp
except ImportError:
    print("ERROR: 'yt-dlp' library not found. Install with: pip install yt-dlp", file=sys.stderr)
    sys.exit(1)

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())  # Avoid "No handler" warnings when library is imported

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"
_YOUTUBE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


def _locate_ffmpeg() -> Tuple[Optional[Path], bool]:
    """Find ffmpeg either on PATH or via imageio-ffmpeg."""
    LOGGER.debug("Searching for ffmpeg on PATH.")
    binary = shutil.which("ffmpeg")
    if binary:
        LOGGER.info("ffmpeg located on PATH at %s", binary)
        return Path(binary), True

    try:
        import imageio_ffmpeg
    except ImportError:
        LOGGER.debug("imageio-ffmpeg not installed; ffmpeg unavailable.")
        return None, False

    try:
        downloaded = Path(imageio_ffmpeg.get_ffmpeg_exe())
        LOGGER.info("Using ffmpeg from imageio-ffmpeg at %s", downloaded)
        return downloaded, True
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("Failed to provision ffmpeg via imageio-ffmpeg: %s", exc)
        return None, False


FFMPEG_PATH, FFMPEG_AVAILABLE = _locate_ffmpeg()


def _ffmpeg_location_arg() -> Optional[str]:
    if not FFMPEG_PATH:
        return None
    return str(FFMPEG_PATH)


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging for CLI usage."""
    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=resolved, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    LOGGER.setLevel(resolved)

def parse_time_to_seconds(value: Optional[str]) -> Optional[float]:
    """Parse a human-friendly time string (e.g. 1:23:45) into seconds."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric < 0:
            return None
        return numeric

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = yt_dlp.utils.parse_duration(text)
    except Exception:  # pragma: no cover - defensive
        parsed = None

    if parsed is not None:
        return float(parsed)

    try:
        numeric = float(text)
    except ValueError:
        return None
    if numeric < 0:
        return None
    return numeric



def _format_ffmpeg_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    if abs(secs - round(secs)) < 1e-3:
        secs_str = f"{int(round(secs)):02d}"
    else:
        secs_str = f"{secs:06.3f}".rstrip("0").rstrip(".")
        if secs_str == "":
            secs_str = "00"
    return f"{hours:02d}:{minutes:02d}:{secs_str}"

def _next_clip_path(source: Path) -> Path:
    suffix = source.suffix or ".mp4"
    candidate = source.with_name(f"{source.stem}_clip{suffix}")
    counter = 1
    while candidate.exists():
        candidate = source.with_name(f"{source.stem}_clip_{counter}{source.suffix}")
        counter += 1
    return candidate


def _clip_media(source: Path, start: Optional[float], end: Optional[float]) -> Optional[Path]:
    if not FFMPEG_AVAILABLE:
        LOGGER.error("Clipping requested but ffmpeg is not available.")
        return None
    if not source.exists():
        LOGGER.error("Cannot clip %s because the file does not exist.", source)
        return None

    temp_target = _next_clip_path(source)
    command = [str(FFMPEG_PATH or "ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None:
        command += ["-ss", _format_ffmpeg_time(start)]
    command += ["-i", str(source)]

    if end is not None:
        duration = end if start is None else end - start
        if duration <= 0:
            LOGGER.error("Clip end time must be greater than clip start time.")
            return None
        command += ["-t", _format_ffmpeg_time(duration)]

    command += ["-c", "copy", str(temp_target)]
    LOGGER.debug("Running ffmpeg clip command: %s", command)

    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        LOGGER.error("ffmpeg failed to clip %s: %s", source, stderr or stdout or "Unknown error.")
        if temp_target.exists():
            try:
                temp_target.unlink()
            except OSError:
                LOGGER.warning("Failed to remove temporary clip file at %s", temp_target)
        return None

    try:
        temp_target.replace(source)
    except OSError as exc:
        LOGGER.error("Failed to replace original file with clipped media: %s", exc)
        try:
            temp_target.unlink()
        except OSError:
            LOGGER.warning("Failed to remove temporary clip file at %s", temp_target)
        return None

    return source



def _sanitize_opts_for_log(options: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of yt-dlp options safe for logging."""
    sanitized = dict(options)
    if "password" in sanitized:
        sanitized["password"] = "***"
    return sanitized


def _build_ydl_options(
    template: str,
    use_ffmpeg: bool,
    cookies_path: Optional[Path],
    username: Optional[str],
    password: Optional[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construct yt-dlp options based on the environment and request."""
    base_opts: Dict[str, Any] = {
        "outtmpl": template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": True,
        "http_headers": {"User-Agent": _YOUTUBE_USER_AGENT},
    }

    if use_ffmpeg:
        LOGGER.debug("Configuring yt-dlp with ffmpeg merge support.")
        base_opts["format"] = "bv*+ba/b"
        base_opts["merge_output_format"] = "mp4"
        ffmpeg_location = _ffmpeg_location_arg()
        if ffmpeg_location:
            LOGGER.debug("Providing ffmpeg location %s to yt-dlp", ffmpeg_location)
            base_opts["ffmpeg_location"] = ffmpeg_location
    else:
        base_opts["format"] = "best"

    if cookies_path:
        base_opts["cookiefile"] = str(cookies_path)
    if username:
        base_opts["username"] = username
        if password:
            base_opts["password"] = password
    if extra:
        base_opts.update(extra)
    return base_opts


def _determine_output_path(info: Dict[str, Any], ydl: "yt_dlp.YoutubeDL") -> Path:
    """Resolve the final output path reported by yt-dlp."""
    final_name = info.get("_filename")
    if final_name:
        candidate = Path(final_name)
        if candidate.exists():
            return candidate

    requested = info.get("requested_downloads") or []
    for request in requested:
        filepath = request.get("filepath")
        if filepath:
            candidate_path = Path(filepath)
            if candidate_path.exists():
                return candidate_path

    candidate = Path(ydl.prepare_filename(info))
    ext = info.get("ext")
    if ext:
        candidate = candidate.with_suffix(f".{ext}")
    return candidate


def _download_with_opts(
    url: str,
    ydl_opts: Dict[str, Any],
    clip_start_seconds: Optional[float],
    clip_end_seconds: Optional[float],
) -> Optional[Path]:
    """Execute yt-dlp download and optional clipping with provided options."""
    sanitized_opts = _sanitize_opts_for_log(ydl_opts)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        LOGGER.debug("Executing yt-dlp with options: %s", sanitized_opts)
        info = ydl.extract_info(url, download=True)
        file_path = _determine_output_path(info, ydl)
        LOGGER.debug("Resolved file path %s from yt-dlp metadata", file_path)

    LOGGER.info("Downloaded %s -> %s", url, file_path)

    if clip_start_seconds is not None or clip_end_seconds is not None:
        LOGGER.info(
            "Clipping downloaded file %s (start=%s, end=%s)",
            file_path,
            clip_start_seconds,
            clip_end_seconds,
        )
        clipped_path = _clip_media(file_path, clip_start_seconds, clip_end_seconds)
        if clipped_path is None:
            LOGGER.error("Clipping failed; keeping original download but reporting failure.")
            return None
        file_path = clipped_path

    return file_path


def _log_download_error(url: str, err: yt_dlp.utils.DownloadError) -> None:
    """Log download errors with additional ffmpeg guidance when relevant."""
    message = str(err)
    LOGGER.error("Video download failed for %s: %s", url, message)
    if "ffmpeg" in message.lower() and FFMPEG_AVAILABLE:
        LOGGER.warning(
            "ffmpeg was expected at %s but yt-dlp reported it missing. Check that the binary is executable.",
            FFMPEG_PATH,
        )


def download_video(
    url: str,
    output_dir: Path,
    filename: Optional[str] = None,
    cookies_path: Optional[Path] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    clip_start: Optional[float] = None,
    clip_end: Optional[float] = None,
) -> Optional[Path]:
    LOGGER.info("Starting download for %s", url)
    output_dir = Path(output_dir)
    LOGGER.debug("Resolved output directory to %s", output_dir)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.error("Unable to create output directory %s: %s", output_dir, exc)
        return None

    template = str(output_dir / (filename or "%(title)s.%(ext)s"))
    LOGGER.debug("Using output template %s", template)

    resolved_cookies_path: Optional[Path] = None
    if cookies_path:
        candidate = Path(cookies_path)
        if candidate.exists():
            resolved_cookies_path = candidate
            LOGGER.info("Using cookies file at %s", candidate)
        else:
            LOGGER.warning("Cookies path %s does not exist; continuing without cookies.", candidate)

    auth_username = username
    auth_password: Optional[str] = None
    if auth_username:
        LOGGER.info("Using provided username for authentication.")
        if password:
            auth_password = password
        else:
            LOGGER.warning("Username provided without password; yt-dlp may prompt for additional credentials.")
    elif password:
        LOGGER.warning("Password provided without username; ignoring password.")

    clip_start_seconds: Optional[float] = None
    clip_end_seconds: Optional[float] = None

    if clip_start is not None:
        try:
            clip_start_seconds = float(clip_start)
        except (TypeError, ValueError):
            LOGGER.error("Invalid clip start value %s; must be numeric seconds.", clip_start)
            return None
        if clip_start_seconds < 0:
            LOGGER.error("Clip start time must be zero or positive.")
            return None

    if clip_end is not None:
        try:
            clip_end_seconds = float(clip_end)
        except (TypeError, ValueError):
            LOGGER.error("Invalid clip end value %s; must be numeric seconds.", clip_end)
            return None
        if clip_end_seconds <= 0:
            LOGGER.error("Clip end time must be greater than zero.")
            return None

    if (
        clip_start_seconds is not None
        and clip_end_seconds is not None
        and clip_end_seconds <= clip_start_seconds
    ):
        LOGGER.error("Clip end time must be greater than clip start time.")
        return None

    if not FFMPEG_AVAILABLE:
        LOGGER.warning(
            "ffmpeg not detected; falling back to best available single-file download without merging audio/video."
        )

    attempt_configs = [
        ("initial", None),
        ("ipv4", {"force_ipv4": True, "http_chunk_size": 1_048_576, "retries": 5, "fragment_retries": 5}),
    ]

    last_error: Optional[yt_dlp.utils.DownloadError] = None
    for index, (label, extra_opts) in enumerate(attempt_configs):
        if index > 0:
            if not (last_error and "downloaded file is empty" in str(last_error).lower()):
                break
            LOGGER.info(
                "Retrying download for %s forcing IPv4 and chunked transfers after empty file error.",
                url,
            )

        ydl_opts = _build_ydl_options(
            template,
            FFMPEG_AVAILABLE,
            resolved_cookies_path,
            auth_username,
            auth_password,
            extra_opts,
        )

        try:
            return _download_with_opts(url, ydl_opts, clip_start_seconds, clip_end_seconds)
        except yt_dlp.utils.DownloadError as err:
            last_error = err
            message_lower = str(err).lower()
            if index == 0 and "downloaded file is empty" in message_lower:
                LOGGER.warning(
                    "Initial download attempt for %s resulted in an empty file. Retrying with IPv4 fallback.",
                    url,
                )
                continue
            _log_download_error(url, err)
            return None
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("Unexpected error during video download for %s", url)
            return None

    if last_error:
        _log_download_error(url, last_error)
    return None


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Download a single video using yt-dlp.")
    parser.add_argument("url", help="Video URL to download")
    parser.add_argument("--output-dir", default="downloads", help="Directory for saved videos")
    parser.add_argument("--filename", help="Optional base filename without extension")
    parser.add_argument("--cookies-file", help="Path to a cookies file in Netscape format")
    parser.add_argument("--username", help="Username for sites that require sign-in")
    parser.add_argument("--password", help="Password for sites that require sign-in")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"],
        help="Logging verbosity for the downloader",
    )

    args = parser.parse_args()
    configure_logging(args.log_level)
    result = download_video(
        args.url,
        Path(args.output_dir),
        args.filename,
        Path(args.cookies_file) if args.cookies_file else None,
        args.username,
        args.password,
    )
    if result:
        print(result)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())




