import re

from services import background_manager as backgrounds
from schemas.pipeline import Record, ReferenceAnalysis, ScriptPlan
from .common import CONFIG, dump, read, vision


def tokens(text):
    return re.findall(r"[^\W\d_]+", text.casefold().replace("ё", "е"))


def retime_speech(scene, spoken_text):
    beats = scene.screen_beats or []
    weights = [len(beat.speech.split()) for beat in beats]
    if not any(weights):
        return
    words = spoken_text.split()
    total = sum(weights)
    previous = cumulative = 0
    for beat, weight in zip(beats, weights):
        cumulative += weight
        end = round(cumulative * len(words) / total)
        beat.speech = " ".join(words[previous:end])
        previous = end


def split_opening(text):
    opening, separator, rest = text.strip().partition("\n")
    if separator:
        return opening.strip(), rest.strip()
    parts = re.split(r"(?<=[.!?…])\s+", text.strip(), maxsplit=1)
    return parts[0], parts[1] if len(parts) == 2 else ""


def cover_repeats_opening(cover, opening):
    words = tokens(cover)
    opening_words = tokens(opening)
    return bool(words) and any(opening_words[i:i + len(words)] == words
                               for i in range(len(opening_words) - len(words) + 1))


def write(directory, bg, brief="", feedback="", *, bg_mode=None):
    analysis = ReferenceAnalysis.model_validate(read(directory / "2_analysis.json"))
    if not analysis.topic_and_tools.strip():
        raise ValueError("Тему референса не удалось подтвердить. Открой «Текст из референса».")
    draft_plan(directory, analysis.model_dump(), bg, brief, feedback, bg_mode=bg_mode)


HOOK_STYLES = {"auto": "Авто", "pain": "Боль и риск", "mistake": "Ошибка",
               "case": "Личный кейс", "numbers": "Цифры"}


def generate_from_topic(topic: str, notes: str = "", hook_style: str = "auto", *,
                        directory, feedback="", bg="studio_dark"):
    draft_plan(directory, {"topic": topic, "hook_style": HOOK_STYLES[hook_style]}, bg, notes,
               feedback, topic_mode=True)


def draft_plan(directory, meaning, bg, brief, feedback, *, bg_mode=None, topic_mode=False):
    brand_voice = (CONFIG / "brand_voice.md").read_text()
    prompt = (
        "Ты пишешь сценарий экспертного Reels от лица автора. Следуй голосу из бренд-гайда. "
        "Сохрани тему, названия продуктов, порядок практических действий и их результат из meaning. "
        "Если референс про Claude и Codex, говори именно о Claude и Codex и покажи нужные интерфейсы. "
        "Не заменяй инструменты и шаги бытовыми аналогиями. Не добавляй кафе, стройку, бумагу, "
        "реквизит или посторонний пример, если их нет в самой теме. Это касается реплик, плашек, "
        "описания каждого кадра и заданий на перебивки. Уточнения автора — бриф и обратная связь. "
        "Перепиши подачу своими словами. Начни с конкретного риска или результата, убери воду. "
        "Пиши простыми разговорными фразами для речи на камеру, без штампов и выдуманных утверждений. "
        "Сделай 5–8 логических сцен. spoken_text — цельная реплика из 1–3 предложений на сцену "
        "для озвучивания HeyGen, без режиссёрских ремарок. "
        "screen_beats — план изображения и приблизительные отрезки речи для предпросмотра. "
        "Не дроби речь на отдельные слова; она должна читаться цельно в spoken_text. "
        "Показывай ведущую, соответствующий экран, подтверждённое действие и результат. "
        "Если экранный материал нужно снять, укажи конкретное окно или шаг, который надо записать. "
        "Белые плашки обозначают шаг или результат. Оставь пустую строку, если надпись не нужна. "
        "Придумай cover_hook_text для кадра 0: 4–7 слов, максимум 50 символов, боль, риск или выгода. "
        "Обложка дополняет первую реплику. Не выдумывай сроки, числа, обещания и срочность. "
        "cover_hook_duration_sec=2.5. По умолчанию music.asset=null.\n" + brand_voice
    )
    if topic_mode:
        prompt += (
            "\nСоздай сценарий по теме пользователя, без референса. Учитывай hook_style. "
            "Верни ровно три alternative_hooks: разные spoken_hook (по одному предложению) и "
            "три разных cover_hook_text. Выбери лучший, запиши его индекс 0–2 в selected_hook. "
            "Начни первую сцену выбранным spoken_hook, а cover_hook_text возьми из той же пары. "
            "Все три захода должны естественно переходить к остальной части первой сцены. "
            "Цифры и личный опыт используй только из брифа или явно обозначь пример как условный."
        )
    presets = backgrounds.studio_library() if bg_mode == "auto_scene" else backgrounds.library()
    if bg_mode in {"auto_match", "auto_scene", "remotion_greenscreen"}:
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
    data = {"meaning": meaning, "brief": brief, "feedback": feedback,
            "preferred_background": bg, "presets": presets}
    plan = vision(prompt, data, ScriptPlan)
    if not 5 <= len(plan.scenes) <= 8:
        raise ValueError("Сценарист должен вернуть от 5 до 8 цельных сцен.")
    if topic_mode:
        if not plan.alternative_hooks or len(plan.alternative_hooks) != 3 or plan.selected_hook is None:
            raise ValueError("Сценарист должен вернуть три варианта хука и индекс выбранной пары.")
        selected = plan.alternative_hooks[plan.selected_hook]
        opening, _ = split_opening(plan.scenes[0].spoken_text)
        if tokens(opening) != tokens(selected.spoken_hook):
            raise ValueError("Первая реплика должна начинаться с выбранного хука.")
        plan.cover_hook_text = selected.cover_hook_text
    opening, _ = split_opening(plan.scenes[0].spoken_text)
    if (not plan.cover_hook_text or not 4 <= len(plan.cover_hook_text.split()) <= 7
            or cover_repeats_opening(plan.cover_hook_text, opening)):
        raise ValueError("Обложке нужен отдельный заголовок из 4–7 слов.")
    if any(not scene.spoken_text.strip() for scene in plan.scenes):
        raise ValueError("В каждой сцене нужна цельная реплика.")
    for index, scene in enumerate(plan.scenes):
        if bg_mode == "ai_generate":
            scene.background_type = "image"
            continue
        if bg_mode == "preset" or (bg_mode is None and index == 0):
            scene.background_prompt_or_asset = bg
        if bg_mode in {"auto_match", "auto_scene", "remotion_greenscreen"} and scene.background_prompt_or_asset not in presets:
            scene.background_prompt_or_asset = bg
        if scene.background_prompt_or_asset in presets:
            scene.background_type = presets[scene.background_prompt_or_asset]["type"]
    dump(directory / "3_script_plan.json", plan.model_dump())


class BackgroundChoice(Record):
    scene_id: int
    preset: str


class BackgroundChoices(Record):
    scenes: list[BackgroundChoice]


def assign_backgrounds(plan, presets, default):
    response = vision(
        "Подбери каждой утверждённой сцене один фон из presets по её смыслу. "
        "Сохраняй фон, пока мысль продолжается. Меняй фон на смене темы. "
        "Верни только scene_id и ключ пресета. Реплики и кадрирование сохраняет программа.",
        {"scenes": [{"scene_id": scene.scene_id, "idea": scene.spoken_text} for scene in plan.scenes],
         "presets": {key: spec.get("description", key) for key, spec in presets.items()}},
        BackgroundChoices,
    )
    choices = {item.scene_id: item.preset for item in response.scenes}
    for scene in plan.scenes:
        name = choices.get(scene.scene_id, default)
        if name not in presets:
            name = default
        scene.background_prompt_or_asset = name
        scene.background_type = presets[name]["type"]
