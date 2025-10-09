"""
Minimal video downloader for sites supported by yt-dlp.
"""
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


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
_YOUTUBE_EXTRACTOR_ARGS = "youtube:player_client=android,web"


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


def _locate_yt_dlp_executable() -> Optional[str]:
    """Locate the yt-dlp executable for CLI usage."""
    try:
        exec_path = yt_dlp.utils.exe_path()
    except AttributeError:
        exec_path = None
    if exec_path:
        return exec_path
    binary = shutil.which("yt-dlp")
    if binary:
        return binary
    return None


YT_DLP_EXECUTABLE = _locate_yt_dlp_executable()


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



def _sanitize_cli_args(args: List[str]) -> List[str]:
    """Mask sensitive values for logging CLI invocations."""
    sanitized: List[str] = []
    mask_next = False
    for item in args:
        if mask_next:
            sanitized.append("***")
            mask_next = False
            continue
        sanitized.append(item)
        if item in {"--password"}:
            mask_next = True
    return sanitized


def _build_cli_command(
    url: str,
    template: str,
    use_ffmpeg: bool,
    cookies_path: Optional[Path],
    username: Optional[str],
    password: Optional[str],
    extra_flags: Optional[List[str]] = None,
) -> List[str]:
    """Assemble the yt-dlp CLI command mirroring downloader.py behaviour."""
    if YT_DLP_EXECUTABLE:
        command: List[str] = [YT_DLP_EXECUTABLE]
    else:
        command = [sys.executable, "-m", "yt_dlp"]

    format_selector = "bv*+ba/b" if use_ffmpeg else "best"
    command.extend(
        [
            url,
            "-f",
            format_selector,
            "--no-playlist",
            "--no-write-info-json",
            "--no-simulate",
            "--no-abort-on-error",
            "--no-part",
            "--force-overwrites",
            "--user-agent",
            _YOUTUBE_USER_AGENT,
            "--extractor-args",
            _YOUTUBE_EXTRACTOR_ARGS,
            "-o",
            template,
            "--print",
            "after_move:filepath",
        ]
    )

    if use_ffmpeg:
        command.extend(["--merge-output-format", "mp4"])
        ffmpeg_location = _ffmpeg_location_arg()
        if ffmpeg_location:
            command.extend(["--ffmpeg-location", ffmpeg_location])

    if cookies_path:
        command.extend(["--cookies", str(cookies_path)])

    if username:
        command.extend(["--username", username])
        if password:
            command.extend(["--password", password])

    if extra_flags:
        command.extend(extra_flags)

    return command


def _parse_filepath_from_output(output: str) -> Optional[Path]:
    """Extract the final filepath emitted via --print after_move:filepath."""
    for line in reversed(output.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        if candidate.lower().startswith("yt-dlp"):
            continue
        path = Path(candidate)
        if path.exists():
            return path
        # Keep the raw candidate; caller will verify existence.
        if path.parts:
            return path
    return None


def _run_cli_download(command: List[str], output_dir: Path) -> Tuple[Optional[Path], str, str, Optional[str]]:
    """Execute yt-dlp via subprocess and capture the resulting file path."""
    sanitized = _sanitize_cli_args(command)
    LOGGER.debug("Running yt-dlp CLI: %s", sanitized)
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    if completed.returncode != 0:
        message = (stderr.strip() or stdout.strip() or f"yt-dlp exited with code {completed.returncode}")
        return None, stdout, stderr, message

    file_path = _parse_filepath_from_output(stdout)
    if file_path and not file_path.exists():
        # Attempt to resolve relative paths inside output_dir
        potential = output_dir / file_path.name
        if potential.exists():
            file_path = potential

    if not file_path or not file_path.exists():
        message = "yt-dlp did not report an output file."
        return None, stdout, stderr, message

    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return None, stdout, stderr, f"Unable to access downloaded file {file_path}: {exc}"

    if size == 0:
        try:
            file_path.unlink()
        except OSError:
            pass
        return None, stdout, stderr, "The downloaded file is empty"

    return file_path, stdout, stderr, None


def _log_download_error(url: str, message: str) -> None:
    """Log download errors with additional ffmpeg guidance when relevant."""
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
        ("initial", []),
        ("ipv4", ["--force-ipv4", "--http-chunk-size", "1048576"]),
    ]

    last_error_message: Optional[str] = None
    last_stdout: str = ""
    last_stderr: str = ""

    for index, (label, extra_flags) in enumerate(attempt_configs):
        if index > 0:
            if not (last_error_message and "downloaded file is empty" in last_error_message.lower()):
                break
            LOGGER.info(
                "Retrying download for %s forcing IPv4 and chunked transfers after empty file error.",
                url,
            )

        command = _build_cli_command(
            url,
            template,
            FFMPEG_AVAILABLE,
            resolved_cookies_path,
            auth_username,
            auth_password,
            extra_flags,
        )

        file_path, stdout, stderr, error_message = _run_cli_download(command, output_dir)
        if file_path:
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

        last_error_message = error_message or "Unknown download error."
        last_stdout = stdout
        last_stderr = stderr

        if index == 0 and last_error_message and "downloaded file is empty" in last_error_message.lower():
            LOGGER.warning(
                "Initial download attempt for %s resulted in an empty file. Retrying with IPv4 fallback.",
                url,
            )
            continue

        if last_error_message:
            _log_download_error(url, last_error_message)
        if last_stdout.strip():
            LOGGER.debug("yt-dlp stdout:\n%s", last_stdout.strip())
        if last_stderr.strip():
            LOGGER.debug("yt-dlp stderr:\n%s", last_stderr.strip())
        return None

    if last_error_message:
        _log_download_error(url, last_error_message)
        if last_stdout.strip():
            LOGGER.debug("yt-dlp stdout:\n%s", last_stdout.strip())
        if last_stderr.strip():
            LOGGER.debug("yt-dlp stderr:\n%s", last_stderr.strip())
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




