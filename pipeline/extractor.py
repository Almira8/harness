import math
import re
import tempfile
from pathlib import Path

from app.media import probe
from schemas.pipeline import Cut, Extraction
from .common import dump, ffmpeg, whisper


def extract(ref, directory):
    ref = Path(ref).resolve(strict=True)
    metadata = probe(ref)
    duration = float(metadata["format"]["duration"])
    preview = directory / "preview.mp4"
    ffmpeg("-i", ref, "-map", "0:v:0", "-an", "-vf",
           "scale='if(gte(iw,ih),-2,720)':'if(gte(iw,ih),720,-2)',setsar=1",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", 26, "-pix_fmt", "yuv420p", preview)
    log = ffmpeg("-i", preview, "-vf", "select='gt(scene,0.27)',showinfo", "-an", "-f", "null", "-")
    starts = [0.0] + sorted({float(s) for s in re.findall(r"pts_time:([\d.]+)", log)
                             if 0 < float(s) < duration})
    # Supplement cuts so a stationary presenter with changing graphics is still readable.
    times = sorted(set(starts + list(range(1, min(4, math.ceil(duration)))) +
                       list(range(4, math.ceil(duration), 3))))
    frames = Path(tempfile.mkdtemp(prefix="frames-", dir=directory))
    by_time = {}
    for i, t in enumerate(times):
        path = frames / f"{i:05}.jpg"
        stamp = f"{int(t)//60:02}\\:{int(t)%60:02}"
        ffmpeg("-ss", min(t,max(0,duration-.1)), "-i", preview, "-frames:v", 1, "-vf",
               f"scale=360:480:force_original_aspect_ratio=decrease,pad=360:480:(ow-iw)/2:(oh-ih)/2,"
               f"drawtext=text='{stamp}':fontcolor=white:fontsize=21:box=1:boxcolor=black:x=5:y=5", path)
        if not path.is_file():
            raise ValueError(f"Не удалось извлечь кадр {t:.2f} с")
        by_time[t] = str(path.relative_to(directory))
    cols = min(5, len(times))
    ffmpeg("-i", frames / "%05d.jpg", "-vf", f"tile={cols}x{math.ceil(len(times)/cols)}",
           "-frames:v", 1, "-q:v", 3, directory / "contact_sheet.jpg")
    audio = None
    phrases = []
    if any(s["codec_type"] == "audio" for s in metadata["streams"]):
        audio = "audio.wav"
        ffmpeg("-i", ref, "-vn", "-ac", 1, "-ar", 16000, directory / audio)
        phrases = whisper(directory / audio, "segment")
    extraction = Extraction(duration=duration, preview=preview.name, audio=audio,
        contact_sheet="contact_sheet.jpg", transcript=phrases,
        words_per_minute=round(60*sum(len(p.text.split()) for p in phrases)/duration, 1),
        cuts=[Cut(start=t, end=end, frame=by_time[t]) for t,end in zip(starts, starts[1:]+[duration])])
    dump(directory / "1_extracted.json", extraction.model_dump())
