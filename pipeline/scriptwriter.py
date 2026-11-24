import difflib
import re

from schemas.pipeline import ReferenceAnalysis, ScriptPlan
from .common import CONFIG, dump, read, vision


def write(directory, bg, brief="", feedback=""):
    analysis = ReferenceAnalysis.model_validate(read(directory / "2_analysis.json"))
    prompt = ("Создай оригинальный практический Reels. Сохрани решаемую задачу, "
        "способ и результат; придумывай новую драматургию и собственные реплики. Привяжи визуал "
        "к конкретной мысли. Используй перечисленные пресеты с правильным типом. Предпочтительный фон "
        "используй в первой сцене. Не выдумывай пути к файлам и музыкальным трекам. "
        "По умолчанию music.asset=null.\n" + (CONFIG / "brand_voice.md").read_text())
    presets = read(CONFIG/"backgrounds.json")
    if bg not in presets:
        raise ValueError(f"Неизвестный пресет --bg: {bg}")
    data = {"meaning": analysis.model_dump(exclude={"original_transcript"}), "brief": brief,
         "feedback": feedback, "preferred_background": bg, "presets": presets,
         "previous_plan": read(directory/"3_script_plan.json") if feedback and (directory/"3_script_plan.json").exists() else None}
    source_words = re.findall(r"\w+", " ".join(p.text for p in analysis.original_transcript).casefold())
    for _ in range(2):
        plan = vision(prompt,data,ScriptPlan)
        script_words = re.findall(r"\w+", " ".join(s.spoken_text for s in plan.scenes).casefold())
        match = difflib.SequenceMatcher(None,source_words,script_words,autojunk=False).find_longest_match()
        if match.size < 10:
            break
        data["revision"] = "В черновике обнаружено длинное дословное совпадение. Перестрой объяснение."
    else:
        raise ValueError("Две версии содержат длинное дословное совпадение с референсом")
    plan.scenes[0].background_type = presets[bg]["type"]
    plan.scenes[0].background_prompt_or_asset = bg
    dump(directory / "3_script_plan.json", plan.model_dump())
