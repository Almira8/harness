import re

from services import background_manager as backgrounds
from schemas.pipeline import Record, ReferenceAnalysis, ScriptPlan
from .common import CONFIG, dump, read, vision


class TopicCheck(Record):
    supported: bool
    correction: str


def word_windows(text, size=6):
    words = re.findall(r"[^\W_]+", text.casefold().replace("ё", "е"))
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def write(directory, bg, brief="", feedback="", *, bg_mode=None):
    analysis = ReferenceAnalysis.model_validate(read(directory / "2_analysis.json"))
    if analysis.topic_supported is False and not feedback.strip():
        raise ValueError("Тему референса не удалось подтвердить. Открой «Текст из референса» и укажи тему через «Запросить правки».")
    prompt = ("Создай оригинальный практический Reels. Сохрани решаемую задачу, "
        "способ, результат и концепцию демонстрации. Напиши новый сценарий с чистого листа: "
        "выбери другой заход в тему, заново построй фразы и связки, сформулируй свой финал. "
        "Замена слов синонимами и пересказ реплика за репликой не подходят. Если порядок шагов "
        "важен для результата, сохрани его. Названия инструментов, команды и термины можно сохранить. "
        "Самостоятельно сформулируй заголовок, подпись и экранные плашки. "
        "Повтори идею показа, предложив собственную композицию и графику. "
        "Хук должен называть практический результат или конкретную проблему. "
        "Дай последовательные выполнимые шаги, сохрани названия инструментов, подтверждённые разбором. "
        "Подстрой объём речи под темп и длительность референса, если пользователь не задал другое. "
        "Реплики предназначены для синтеза: без Markdown и режиссёрских ремарок. Привяжи визуал "
        "к конкретной мысли. Не выдумывай пути к файлам и музыкальным трекам. "
        "Для каждой сцены заполни screen_beats: каждый элемент — одна примерная секунда. "
        "Разбей spoken_text на короткие фразы по порядку, без пропусков, добавлений и повторов; "
        "для паузы используй пустой speech. В action напиши, что буквально увидит зритель: "
        "кто или что в кадре, какая крупность, какой именно предмет или результат показан. "
        "Если кадр держится, прямо укажи, что именно остаётся видно. "
        "В screen_text укажи точную надпись на эту секунду или пустую строку. "
        "Используй надпись из overlay_text. В animation опиши, что появляется, движется или исчезает, "
        "либо оставь пустую строку. Текст вставки белый, движение под новый сценарий. "
        "Различай кадр ведущей и предложенную перебивку; для перебивки назови нужный материал. "
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
    extraction_path = directory / "1_extracted.json"
    extraction = read(extraction_path) if extraction_path.exists() else {}
    speech = extraction.get("transcript") or [phrase.model_dump() for phrase in analysis.original_transcript]
    screen_text = extraction.get("screen_text", [])
    source_texts = [" ".join(phrase["text"] for phrase in speech)]
    source_texts.extend(line for item in screen_text for line in item["text"].splitlines())
    source_windows = set().union(*(word_windows(text) for text in source_texts))
    quoted_windows = set().union(*(word_windows(text, size=5) for text in source_texts))
    meaning = {}
    for key in ("core_message", "narrative_arc"):
        sentences = re.split(r"(?<=[.!?;])\s+|\n+", getattr(analysis, key))
        meaning[key] = " ".join(sentence for sentence in sentences
                                if not quoted_windows & word_windows(sentence, size=5))
    if not any(meaning.values()) and not feedback.strip():
        raise ValueError("Не удалось выделить идею отдельно от слов референса. Укажи главную мысль через «Запросить правки».")
    data = {"meaning": meaning, "brief": brief, "feedback": feedback,
            "preferred_background": bg, "presets": presets}
    if extraction:
        data["reference_duration"] = extraction["duration"]
        data["reference_words_per_minute"] = extraction["words_per_minute"]
    verify_topic = analysis.topic_supported is not None
    for _ in range(2):
        plan = vision(prompt,data,ScriptPlan)
        draft_text = [" ".join(s.spoken_text for s in plan.scenes), plan.title, plan.caption,
                      *(s.overlay_text or "" for s in plan.scenes),
                      *(beat.screen_text for scene in plan.scenes for beat in (scene.screen_beats or []))]
        if any(source_windows & word_windows(text) for text in draft_text):
            data["revision"] = (
                "В черновике найдено дословное совпадение из шести и более слов. "
                "Напиши новый хук и перестрой фразы целиком, сохранив тему, шаги и результат."
            )
            continue
        if verify_topic:
            check = vision(
                "Проверь, что черновик сохраняет основную задачу, метод и результат исходника. "
                "Приоритет у явного уточнения пользователем самой темы. "
                "Отдельно проверь оригинальность хука, речи, заголовка, подписи и плашек. "
                "Поставь supported=false при построчном пересказе, повторе необычной фразы "
                "или сохранении построения реплик с заменой слов синонимами. "
                "Совпадение фактов, обязательных шагов, названий инструментов и терминов допустимо. "
                "При слишком близком черновике укажи, какие фразы перестроить, не меняя задачу. "
                "Если тема исходника не подтверждена и пользователь не назвал её явно, поставь supported=false. "
                "Считай подменой темы выдуманный инструмент, другой процесс или превращение "
                "случайного примера в главную тему. Отметь supported=false при подмене и опиши правку.",
                {"meaning": {**meaning, "topic_supported": analysis.topic_supported},
                 "screen_text": screen_text, "speech": speech,
                 "brief": brief, "feedback": feedback,
                 "draft": {"narration": [s.spoken_text for s in plan.scenes], "title": plan.title,
                           "caption": plan.caption, "overlays": [s.overlay_text for s in plan.scenes]}},
                TopicCheck,
            )
            if not check.supported:
                data["revision"] = (
                    "Начни заново по смысловому разбору. Сохрани задачу, метод и результат, "
                    "но придумай другой хук, перестрой объяснение и напиши новые надписи."
                )
                continue
        break
    else:
        raise ValueError("Не удалось написать сценарий, сохранив тему и оригинальные формулировки. Проверь «Текст из референса» и уточни главное действие.")
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
