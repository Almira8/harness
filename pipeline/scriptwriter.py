from services import background_manager as backgrounds
from schemas.pipeline import Record, ReferenceAnalysis, ScriptPlan
from .common import CONFIG, dump, read, vision


class TopicCheck(Record):
    supported: bool
    correction: str


def write(directory, bg, brief="", feedback="", *, bg_mode=None, needs_rewrite=None):
    analysis = ReferenceAnalysis.model_validate(read(directory / "2_analysis.json"))
    if not (analysis.topic_problem and analysis.core_insight) and not feedback.strip():
        raise ValueError("Тему референса не удалось подтвердить. Открой «Текст из референса» и укажи тему через «Запросить правки».")
    prompt = ("Ты создаёшь оригинальный ролик на основе абстрактной формулы идеи. "
        "Тебе запрещено пересказывать или синонимизировать чужой текст. "
        "Придумай свежий хук и новый жизненный пример или аналог. Объясни core_insight "
        "живым разговорным языком, словно делишься собственным опытом с нуля, "
        "без выдуманных заявлений о реальных событиях. "
        "Не копируй порядок предложений из оригинала. Выстрой естественную речь. "
        "hook_formula и narrative_formula описывают механику удержания. "
        "Используй их свободно, под новый пример. Самостоятельно сформулируй заголовок, подпись и надписи. "
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
    data = {"meaning": analysis.model_dump(), "brief": brief, "feedback": feedback,
            "preferred_background": bg, "presets": presets}
    for _ in range(2):
        plan = vision(prompt, data, ScriptPlan, temperature=0.75)
        if needs_rewrite is not None and needs_rewrite(plan):
            data["revision"] = (
                "Предыдущий черновик слишком похож на исходный или прежний текст. "
                "Зайди с другой стороны, придумай новую бытовую ситуацию, "
                "измени порядок объяснения и перепиши все реплики и надписи с нуля."
            )
            continue
        check = vision(
            "Проверь черновик по абстрактному разбору. Сохранены ли проблема и принцип решения? "
            "Есть ли конкретный жизненный пример или аналог, который объясняет принцип? "
            "Предпочти явное уточнение автора, если исходная идея не определилась. "
            "Отклони черновик, если он подменяет задачу, метод или результат.",
            {"meaning": data["meaning"], "brief": brief, "feedback": feedback,
             "draft": plan.model_dump()}, TopicCheck,
        )
        if check.supported:
            break
        data["revision"] = "Сохрани исходную задачу и принцип решения. Объясни их через конкретный новый пример."
    else:
        raise ValueError(
            "После повторной попытки сценарий остался слишком похож на исходный текст "
            "или отклонился от идеи. Черновик не сохранён."
        )
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
