import argparse
from pathlib import Path
from uuid import uuid4

from pipeline import analyzer, composer, extractor, scriptwriter, visuals
from pipeline.common import dump, read, workspace
from pipeline.originality import check_draft
from services.background_manager import MODES


def main():
    parser = argparse.ArgumentParser(description="Reels Studio · пять этапов")
    parser.add_argument("--ref", type=Path)
    parser.add_argument("--bg", metavar="PRESET", help="Пресет для всех сцен в preset или запасной вариант при автоподборе")
    parser.add_argument("--bg-mode", choices=MODES, help=(
        "preset — один пресет; auto_match — выбор для каждой сцены; "
        "ai_generate — уникальные кадры OpenAI Image; "
        "remotion_greenscreen — хромакей и анимированный фон Remotion. "
        "По умолчанию preset для новых выпусков; при продолжении берётся режим выпуска."))
    parser.add_argument("--aspect", choices=["9:16","16:9"])
    parser.add_argument("--job-id")
    parser.add_argument("--from-step", type=int, choices=range(1,6), default=1)
    parser.add_argument("--only-step", type=int, choices=range(1,6))
    parser.add_argument("--until-step", type=int, choices=range(1,6), default=5)
    args = parser.parse_args()
    start = args.only_step or args.from_step
    end = args.only_step or args.until_step
    if start > end or (start>1 and not args.job_id):
        parser.error("Для продолжения укажите --job-id и корректный диапазон этапов")
    if start == 5 and (args.bg or args.bg_mode):
        parser.error("Чтобы изменить фон, запустите этап 4; для автоподбора заново подготовьте сценарий на этапе 3")
    with workspace(args.job_id or uuid4().hex) as d:
        print(f"workspace: {d}",flush=True)
        meta = read(d/"job.json") if (d/"job.json").exists() else {}
        if args.bg_mode:
            meta["bg_mode"] = args.bg_mode
        elif not meta and start == 1:
            meta["bg_mode"] = "preset"
        if args.ref:
            meta["ref"] = str(args.ref.resolve(strict=True))
        meta["aspect"] = args.aspect or meta.get("aspect","9:16")
        meta["bg"] = args.bg or meta.get("bg","studio_dark")
        dump(d/"job.json",meta)
        for step in range(start,end+1):
            print(f"Этап {step}",flush=True)
            if step == 1:
                if not meta.get("ref"):
                    parser.error("Укажите --ref для извлечения")
                extractor.extract(meta["ref"],d)
            elif step == 2:
                analyzer.analyze(d)
            elif step == 3:
                if start == 3 and not args.only_step and (d/"3_script_plan.json").exists():
                    print("Используется сохранённый сценарий")
                else:
                    if analyzer.needs_analysis(d):
                        analyzer.analyze(d)
                    scriptwriter.write(d, meta["bg"], bg_mode=meta.get("bg_mode"),
                                       needs_rewrite=check_draft(d), check_source=analyzer.draft_check(d))
            elif step == 4:
                visuals.render(d, meta["aspect"], bg_mode=meta.get("bg_mode"), bg=meta["bg"])
            else:
                composer.compose(d, render_only=start == 5)


if __name__ == "__main__":
    main()
