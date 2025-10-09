"""
Minimal video downloader for sites supported by yt-dlp.
"""
import logging
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

try:
    import yt_dlp
except ImportError:
    print("ERROR: 'yt-dlp' library not found. Install with: pip install yt-dlp", file=sys.stderr)
    sys.exit(1)

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())  # Avoid "No handler" warnings when library is imported

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"


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

    if FFMPEG_AVAILABLE:
        LOGGER.debug("Configuring yt-dlp with ffmpeg merge support.")
        ydl_opts = {
            "outtmpl": template,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        ffmpeg_location = _ffmpeg_location_arg()
        if ffmpeg_location:
            LOGGER.debug("Providing ffmpeg location %s to yt-dlp", ffmpeg_location)
            ydl_opts["ffmpeg_location"] = ffmpeg_location
    else:
        LOGGER.warning(
            "ffmpeg not detected; falling back to best available single-file download without merging audio/video."
        )
        ydl_opts = {
            "outtmpl": template,
            "format": "best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

    if cookies_path:
        cookies_path = Path(cookies_path)
        if cookies_path.exists():
            ydl_opts["cookiefile"] = str(cookies_path)
            LOGGER.info("Using cookies file at %s", cookies_path)
        else:
            LOGGER.warning("Cookies path %s does not exist; continuing without cookies.", cookies_path)

    if username:
        ydl_opts["username"] = username
        LOGGER.info("Using provided username for authentication.")
        if password:
            ydl_opts["password"] = password
        else:
            LOGGER.warning("Username provided without password; yt-dlp may prompt for additional credentials.")
    elif password:
        LOGGER.warning("Password provided without username; ignoring password.")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            LOGGER.debug("Executing yt-dlp with options: %s", ydl_opts)
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

            
            info = ydl.extract_info(url, download=True)
            requested = info.get("requested_downloads")
            if requested:
                file_path = Path(requested[0]["filepath"])
                LOGGER.debug("yt-dlp reported requested download path %s", file_path)
            else:
                file_path = Path(ydl.prepare_filename(info))
                ext = info.get("ext")
                if ext:
                    file_path = file_path.with_suffix(f".{ext}")
                LOGGER.debug("Derived file path %s using metadata", file_path)
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
    except yt_dlp.utils.DownloadError as err:
        message = str(err)
        LOGGER.error("Video download failed for %s: %s", url, message)
        if "ffmpeg" in message.lower() and FFMPEG_AVAILABLE:
            LOGGER.warning(
                "ffmpeg was expected at %s but yt-dlp reported it missing. Check that the binary is executable.",
                FFMPEG_PATH,
            )
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("Unexpected error during video download for %s", url)
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


