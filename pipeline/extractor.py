import math
import re
import tempfile
from pathlib import Path

from app.media import probe
from schemas.pipeline import Cut, Extraction, Sample
from .common import dump, ffmpeg, whisper
from .ocr import read_frames


NO_REFERENCE_TEXT = (
    "В этом видеофайле не найдена речь и текст на экране. Проверьте, записался ли звук в .mov"
)


def speech_word_count(phrases):
    # Count words with at least two letters, excluding punctuation, digits and one-letter tokens.
    return sum(len(re.findall(r"[^\W\d_]{2,}(?:[-’'][^\W\d_]+)*", phrase.text))
               for phrase in phrases)


def require_reference_text(phrases, screen_text):
    if speech_word_count(phrases) < 5 and not any(item.text.strip() for item in screen_text):
        raise ValueError(NO_REFERENCE_TEXT)


def extract(ref, directory):
    ref = Path(ref).resolve(strict=True)
    metadata = probe(ref)
    video = next((stream for stream in metadata["streams"] if stream["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("В референсе нет видеодорожки")
    duration = float(video.get("duration") or metadata["format"]["duration"])
    preview = directory / "preview.mp4"
    ffmpeg("-i", ref, "-map", "0:v:0", "-an", "-vf",
           "scale='if(gte(iw,ih),-2,720)':'if(gte(iw,ih),720,-2)',setsar=1",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", 26, "-pix_fmt", "yuv420p", preview)
    log = ffmpeg("-i", preview, "-vf", "select='gt(scene,0.27)',showinfo", "-an", "-f", "null", "-")
    starts = [0.0] + sorted({float(s) for s in re.findall(r"pts_time:([\d.]+)", log)
                             if 0 < float(s) < duration})
    # Bound vision input and cover the whole timeline, including stationary demonstrations.
    sample_count = min(18, max(1, math.ceil(duration / 2)))
    regular = [i * duration / sample_count for i in range(sample_count)]
    opening = [t for t in (0.8, 1.8, 2.8) if t < duration]
    cuts = sorted(starts[1:], key=lambda t: min(abs(t - s) for s in regular), reverse=True)
    times = sorted(set(regular + opening + cuts[:6]))
    frames = Path(tempfile.mkdtemp(prefix="frames-", dir=directory))
    by_time = {}
    for i, t in enumerate(times):
        path = frames / f"{i:05}.jpg"
        ffmpeg("-ss", min(t,max(0,duration-.1)), "-i", ref, "-map", "0:v:0", "-frames:v", 1, "-vf",
               "scale='min(1920,iw)':'min(1920,ih)':force_original_aspect_ratio=decrease,setsar=1",
               "-q:v", 2, path)
        if not path.is_file():
            raise ValueError(f"Не удалось извлечь кадр {t:.2f} с")
        by_time[t] = str(path.relative_to(directory))
    cols = min(5, len(times))
    ffmpeg("-i", frames / "%05d.jpg", "-vf", f"scale=240:320:force_original_aspect_ratio=decrease,pad=240:320:(ow-iw)/2:(oh-ih)/2,tile={cols}x{math.ceil(len(times)/cols)}",
           "-frames:v", 1, "-q:v", 3, directory / "contact_sheet.jpg")
    audio = None
    phrases = []
    has_audio = any(s["codec_type"] == "audio" for s in metadata["streams"])
    if has_audio:
        audio = "audio.wav"
        ffmpeg("-i", ref, "-vn", "-ac", 1, "-ar", 16000, directory / audio)
        phrases = whisper(directory / audio, "segment")
    samples = [Sample(time=t, frame=by_time[t]) for t in times]
    screen_text, ocr_warning = read_frames(directory, samples)
    require_reference_text(phrases, screen_text)
    if speech_word_count(phrases) < 5:
        phrases = []
    extraction = Extraction(duration=duration, preview=preview.name, audio=audio,
        contact_sheet="contact_sheet.jpg", transcript=phrases,
        words_per_minute=round(60*sum(len(p.text.split()) for p in phrases)/duration, 1),
        cuts=[Cut(start=t, end=end, frame=by_time.get(t, ""))
              for t, end in zip(starts, starts[1:] + [duration])],
        samples=samples, screen_text=screen_text, ocr_warning=ocr_warning)
    dump(directory / "1_extracted.json", extraction.model_dump())
