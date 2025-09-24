"""
Minimal video downloader using yt-dlp.
"""
import logging
import sys
from pathlib import Path
from typing import Optional

try:
    import yt_dlp
except ImportError:
    print("ERROR: 'yt-dlp' library not found. Install with: pip install yt-dlp", file=sys.stderr)
    sys.exit(1)


def download_video(url: str, output_dir: Path, filename: Optional[str] = None) -> Optional[Path]:
    output_dir = Path(output_dir)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.error("Unable to create output directory %s: %s", output_dir, exc)
        return None

    template = str(output_dir / (filename or "%(title)s.%(ext)s"))

    ydl_opts = {
        "outtmpl": template,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            requested = info.get("requested_downloads")
            if requested:
                file_path = Path(requested[0]["filepath"])
            else:
                file_path = Path(ydl.prepare_filename(info))
                ext = info.get("ext")
                if ext:
                    file_path = file_path.with_suffix(f".{ext}")
            logging.info("Downloaded %s -> %s", url, file_path)
            return file_path
    except yt_dlp.utils.DownloadError as err:
        logging.error("Video download failed for %s: %s", url, err)
    except Exception:
        logging.exception("Unexpected error during video download for %s", url)
    return None


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Download a single video using yt-dlp.")
    parser.add_argument("url", help="Video URL to download")
    parser.add_argument("--output-dir", default="downloads", help="Directory for saved videos")
    parser.add_argument("--filename", help="Optional base filename without extension")

    args = parser.parse_args()
    result = download_video(args.url, Path(args.output_dir), args.filename)
    if result:
        print(result)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
