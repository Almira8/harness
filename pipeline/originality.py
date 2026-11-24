"""Compare drafts with source material without exposing it to the writing model."""
import re

from .common import read

STOP_WORDS = set(
    "а без бы был была были быть в вам вас ведь во вот все вы где да даже для до его ее если есть "
    "еще же за зачем здесь и из или им их к как когда ко кто ли мне мы на над не него нет ни но "
    "ну о об она они оно от по под после при про раз с сам себе себя со так также там те тем то "
    "того тоже только том тот ту ты у уже чем что чтобы эта эти это этот я the and for are that "
    "this with have from your you not was but can into".split()
)


def tokens(text):
    return re.findall(r"[^\W\d_]+", text.casefold().replace("ё", "е"))


def ngrams(words, size=3):
    return set(zip(*(words[offset:] for offset in range(size))))


def significant(words):
    return {word for word in words if len(word) >= 3 and word not in STOP_WORDS}


def overlap(draft_texts, source):
    source_words = tokens(source)
    source_phrases = ngrams(source_words)
    draft_words = [tokens(text) for text in draft_texts]
    if any(ngrams(words) & source_phrases for words in draft_words):
        return True
    a = set().union(*(significant(words) for words in draft_words))
    b = significant(source_words)
    union = a | b
    return bool(union) and len(a & b) / len(union) > 0.25


def check_draft(directory, previous_narrations=()):
    path = directory / "1_extracted.json"
    source = " ".join(item["text"] for item in read(path).get("transcript", [])) if path.exists() else ""
    previous = [tokens(text) for text in previous_narrations if text.strip()]

    def needs_rewrite(plan):
        narration = " ".join(scene.spoken_text for scene in plan.scenes)
        overlays = [scene.overlay_text or "" for scene in plan.scenes]
        overlays.extend(beat.screen_text for scene in plan.scenes for beat in (scene.screen_beats or []))
        if overlap([narration, plan.title, plan.caption, plan.cover_hook_text or "", *overlays], source):
            return True
        words = tokens(narration)
        phrases = ngrams(words)
        for old in previous:
            shared = phrases & ngrams(old)
            if words == old or (len(shared) >= 3 and len(shared) / len(phrases) >= 0.4):
                return True
        return False

    return needs_rewrite
