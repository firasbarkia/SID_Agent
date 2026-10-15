"""python -m app.evaluation.cli {score,init-review} --help"""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from app.evaluation.models import Corpus, Reviews, SavedRun
from app.evaluation.runner import review_template, score


def markdown(report):
    lines = [
        f"# Evaluation: {report['run_id']}",
        "",
        "Synthetic expectations are provisional. Human scores are reported separately.",
        f"Human review: **{report['human_review_status']}**. Release gate: **not assessed**.",
        "",
        "| Split | Cases | Invalid outputs | False supported rows | Human reviews |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for split, item in report["splits"].items():
        lines.append(
            f"| {split} | {item['case_count']} | "
            f"{item['output_validation'].get('invalid_output', 0)} | "
            f"{item['provisional_totals'].get('false_supported_rows', 0)} | "
            f"{item['human_reviewed']}/{item['human_required']} |"
        )
    lines += [
        "",
        "| Case | Workflow | Language | Validation | Provisional observations |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in report["cases"]:
        metrics = json.dumps(row["provisional_metrics"], ensure_ascii=False).replace("|", "\\|")
        lines.append(
            f"| {row['case_id']} | {row['kind']} | {row['language']} | "
            f"{row['validation']} | {metrics} |"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Score saved AI outputs without provider calls")
    parser.add_argument("command", choices=["score", "init-review"])
    parser.add_argument("--corpus", type=Path, default=Path("data/evaluation/corpus-v1.json"))
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    if args.markdown and args.command != "score":
        parser.error("--markdown is only available for score")
    try:
        corpus = Corpus.model_validate_json(args.corpus.read_text("utf-8"))
        run = SavedRun.model_validate_json(args.outputs.read_text("utf-8"))
        if args.command == "init-review":
            result = review_template(corpus, run).model_dump()
        else:
            reviews = (
                Reviews.model_validate_json(args.reviews.read_text("utf-8"))
                if args.reviews
                else None
            )
            result = score(corpus, run, reviews)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if args.markdown:
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(markdown(result), encoding="utf-8")
        print(f"Saved {args.command} artifact: {args.out}")
    except ValidationError as exc:
        parser.exit(2, f"Evaluation input schema invalid ({exc.error_count()} errors)\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Evaluation input error: {exc}\n")


if __name__ == "__main__":
    main()
