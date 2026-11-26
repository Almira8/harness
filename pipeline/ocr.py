"""Read timestamped screen text before interpreting a video."""
import csv
import os
import shutil
import subprocess

from schemas.pipeline import ScreenText


def read_frames(directory, samples):
    binary = shutil.which("tesseract")
    if not binary:
        return [], "OCR недоступен: Tesseract не установлен."
    languages = subprocess.run([binary, "--list-langs"], capture_output=True, text=True,
                               timeout=10, check=True).stdout.splitlines()[1:]
    selected = [lang for lang in ("rus", "eng") if lang in languages]
    if not selected:
        return [], "OCR недоступен: нужны языковые пакеты rus или eng."
    observations = []
    seen = set()
    for sample in samples:
        process = subprocess.run([binary, str(directory / sample.frame), "stdout", "-l",
                                  "+".join(selected), "--psm", "11", "-c", "tessedit_create_tsv=1"],
                                 capture_output=True, text=True, timeout=30, check=True,
                                 env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        lines = {}
        for row in csv.DictReader(process.stdout.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE):
            if row["level"] != "5" or not row["text"].strip() or float(row["conf"]) < 50:
                continue
            key = (row["block_num"], row["par_num"], row["line_num"])
            lines.setdefault(key, []).append(row["text"])
        fresh = []
        for words in lines.values():
            line = " ".join(words)
            key = line.casefold()
            if len(line) >= 3 and key not in seen:
                seen.add(key)
                fresh.append(line)
        if fresh:
            observations.append(ScreenText(time=sample.time, text="\n".join(fresh)[:800]))
    warning = None if "rus" in selected else "Для OCR русского текста установите пакет Tesseract rus."
    return observations, warning
