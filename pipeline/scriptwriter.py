import difflib
import re

from services import background_manager as backgrounds
from schemas.pipeline import ReferenceAnalysis, ScriptPlan
from .common import CONFIG, dump, read, vision


def write(directory, bg, brief="", feedback="", *, bg_mode=None):
    analysis = ReferenceAnalysis.model_validate(read(directory / "2_analysis.json"))
    prompt = ("Создай оригинальный практический Reels. Сохрани решаемую задачу, "
        "способ и результат; придумай новую драматургию и собственные реплики. "
        "Хук должен называть практический результат или конкретную проблему. "
        "Дай последовательные выполнимые шаги, сохрани названия инструментов, подтверждённые разбором. "
        "Подстрой объём речи под темп и длительность референса, если пользователь не задал другое. "
        "Реплики предназначены для синтеза: без Markdown и режиссёрских ремарок. Привяжи визуал "
        "к конкретной мысли. Не выдумывай пути к файлам и музыкальным трекам. "
        "По умолчанию music.asset=null.\n" + (CONFIG / "brand_voice.md").read_text())
    presets = backgrounds.library()
    if bg_mode in {"auto_match", "remotion_greenscreen"}:
        presets.pop("greenscreen", None)
        prompt += ("\nДля каждой сцены выбирай подходящий пресет из presets по его описанию. "
                   "В background_prompt_or_asset впиши только имя пресета. В первой сцене "
                   "можно выбрать яркий акцент, затем перейти на спокойный фон.")
    if bg_mode == "ai_generate":
        prompt += ("\nДля каждой сцены укажи background_type=image и краткое визуальное описание "
                   "новой фоновой композиции в background_prompt_or_asset. "
                   "По этому описанию будет сгенерирован кадр, пути к файлам не указывай.")
    if bg_mode in {None, "preset"}:
        prompt += "\nИспользуй перечисленные пресеты с правильным типом. Начни с preferred_background."
    if bg_mode != "ai_generate" and bg not in presets:
        raise ValueError(f"Неизвестный пресет --bg: {bg}")
    data = {"meaning": analysis.model_dump(exclude={"original_transcript"}), "brief": brief,
         "feedback": feedback, "preferred_background": bg, "presets": presets,
         "previous_plan": read(directory/"3_script_plan.json") if feedback and (directory/"3_script_plan.json").exists() else None}
    if (directory/"1_extracted.json").exists():
        extraction = read(directory/"1_extracted.json")
        data["reference_duration"] = extraction["duration"]
        data["reference_words_per_minute"] = extraction["words_per_minute"]
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
    for index, scene in enumerate(plan.scenes):
        if bg_mode == "ai_generate":
            scene.background_type = "image"
            continue
        if bg_mode == "preset" or (bg_mode is None and index == 0):
            scene.background_prompt_or_asset = bg
        if bg_mode in {"auto_match", "remotion_greenscreen"} and scene.background_prompt_or_asset not in presets:
            scene.background_prompt_or_asset = bg
        if scene.background_prompt_or_asset in presets:
            scene.background_type = presets[scene.background_prompt_or_asset]["type"]
    dump(directory / "3_script_plan.json", plan.model_dump())
