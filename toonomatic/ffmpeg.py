"""Petits utilitaires autour de FFmpeg / ffprobe."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def require() -> None:
    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            raise FFmpegError(f"{exe} introuvable dans le PATH")


def run(args: list[str], quiet: bool = True) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error" if quiet else "info", *map(str, args)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg a échoué ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-2000:]}")


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def escape_filter_path(p: Path) -> str:
    """Échappe un chemin pour l'utiliser dans un filtre (subtitles=, movie=...)."""
    s = str(Path(p).resolve())
    return s.replace("\\", "/").replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")


def escape_drawtext(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(":", r"\:").replace("'", "’")
            .replace("%", r"\%").replace(",", r"\,"))


def concat(files: list[Path], out: Path, reencode: bool = False) -> Path:
    lst = out.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{Path(f).resolve()}'\n" for f in files), encoding="utf-8")
    args = ["-f", "concat", "-safe", "0", "-i", lst]
    args += ["-c", "copy"] if not reencode else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                                  "-pix_fmt", "yuv420p", "-c:a", "aac"]
    run([*args, out])
    lst.unlink(missing_ok=True)
    return out
