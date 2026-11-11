"""Assemblage final avec FFmpeg : scènes → épisode, mixage voix + musique (ducking),
incrustation des sous-titres stylisés, et extrait vertical 9:16 pour les plateformes courtes."""
from __future__ import annotations

from pathlib import Path

from .. import ffmpeg


def assemble_episode(scene_videos: list[Path], dialogue: Path, out: Path, *, ass: Path | None,
                     music: Path | None = None, clean_out: Path | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    picture = ffmpeg.concat(scene_videos, out.with_name(out.stem + "_picture.mp4"))
    inputs = ["-i", picture, "-i", dialogue]
    if music:
        inputs += ["-stream_loop", "-1", "-i", music]
        # la musique baisse automatiquement sous les voix (sidechain)
        af = ("[1:a]aformat=channel_layouts=stereo,asplit=2[vo][sc];"
              "[2:a]aformat=channel_layouts=stereo,volume=0.35[mu];"
              "[mu][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
              "[vo][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-16:TP=-1.5[aout]")
    else:
        af = "[1:a]aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-1.5[aout]"
    clean = clean_out or out.with_name(out.stem + "_clean.mp4")
    ffmpeg.run([*inputs, "-filter_complex", af, "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", 48000, "-shortest", clean])
    if ass:
        ffmpeg.run(["-i", clean, "-vf", f"subtitles='{ffmpeg.escape_filter_path(ass)}'",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", out])
    else:
        clean.replace(out)
    picture.unlink(missing_ok=True)
    return out


def make_cutdown(clean: Path, start: float, end: float, out: Path, *, size=(1080, 1920),
                 ass: Path | None = None) -> Path:
    w, h = size
    vf = f"crop=ih*{w}/{h}:ih,scale={w}:{h}:flags=lanczos,setsar=1"
    if ass:
        vf += f",subtitles='{ffmpeg.escape_filter_path(ass)}'"
    ffmpeg.run(["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", clean, "-vf", vf,
                "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out])
    return out
