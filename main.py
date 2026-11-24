import argparse
from pathlib import Path
from uuid import uuid4

from pipeline import analyzer, composer, extractor, scriptwriter, visuals
from pipeline.common import dump, read, workspace


def main():
    parser = argparse.ArgumentParser(description="Reels Studio · пять этапов")
    parser.add_argument("--ref", type=Path)
    parser.add_argument("--bg", default=None)
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
    with workspace(args.job_id or uuid4().hex) as d:
        print(f"workspace: {d}",flush=True)
        meta = read(d/"job.json") if (d/"job.json").exists() else {}
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
                    scriptwriter.write(d,meta["bg"])
            elif step == 4:
                visuals.render(d,meta["aspect"])
            else:
                composer.compose(d, render_only=start == 5)


if __name__ == "__main__":
    main()
