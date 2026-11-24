import base64
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import wave
from contextlib import contextmanager
from pathlib import Path

from openai import BadRequestError

from app.config import ROOT, settings
from app.agents import client
from schemas.pipeline import Phrase

CONFIG = ROOT / "config"


def dump(path, value):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
    os.replace(output.name, path)


def read(path):
    return json.loads(path.read_text())


def digest(path):
    with path.open("rb") as f:
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def workspace(job_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", job_id):
        raise ValueError("Недопустимый job-id")
    directory = settings.pipeline_workspace.resolve() / job_id
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


def ffmpeg(*args, cwd=None):
    result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-y", *map(str, args)],
                            cwd=cwd, capture_output=True, timeout=1800)
    if result.returncode:
        raise ValueError("FFmpeg: " + result.stderr.decode(errors="replace")[-1800:])
    return result.stderr.decode(errors="replace")


def vision(prompt, data, schema, images=(), *, temperature=None):
    content = [{"type": "input_text", "text": json.dumps(data, ensure_ascii=False)}]
    for path in images:
        content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," +
                        base64.b64encode(path.read_bytes()).decode(), "detail": "high"})
    messages = [{"role": "developer", "content": prompt}, {"role": "user", "content": content}]
    api = client()
    sampling = {"temperature": temperature} if temperature is not None else {}
    try:
        response = api.responses.parse(model=settings.openai_model, text_format=schema, input=messages, **sampling)
    except BadRequestError as error:
        if not sampling or error.param != "temperature":
            raise
        # Reasoning models can accept only their default sampling parameters.
        response = api.responses.parse(model=settings.openai_model, text_format=schema, input=messages)
    if response.status != "completed" or response.output_parsed is None:
        raise ValueError("Модель не завершила ответ")
    return response.output_parsed


def whisper(wav, granularity):
    # Mono PCM at 16 kHz: ten minutes per request stays below the upload limit.
    api = client()
    with tempfile.TemporaryDirectory(dir=wav.parent) as td:
        ffmpeg("-i", wav, "-ac", 1, "-ar", 16000, "-f", "segment", "-segment_time", 600,
               Path(td) / "%04d.wav")
        phrases = []
        offset = 0.0
        for chunk in sorted(Path(td).glob("*.wav")):
            with chunk.open("rb") as audio:
                result = api.audio.transcriptions.create(file=audio, model=settings.transcription_model,
                    language=settings.transcription_language, temperature=0,
                    response_format="verbose_json", timestamp_granularities=[granularity]).model_dump()
            for part in result.get(granularity + "s") or []:
                if (part.get("no_speech_prob") is not None and part["no_speech_prob"] > 0.6
                        and part.get("avg_logprob") is not None and part["avg_logprob"] < -1):
                    continue
                if part["end"] > part["start"]:
                    phrases.append(Phrase(start=offset + part["start"], end=offset + part["end"],
                                          text=part.get("text", part.get("word", "")).strip()))
            with wave.open(str(chunk)) as source:
                offset += source.getnframes() / source.getframerate()
        return phrases
