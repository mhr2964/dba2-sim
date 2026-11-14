"""CLI: the human-verification tool for the newsroom's AI writer
(docs/design/media-v2-step8-anthropic-writer.md S8-15 item 7).

It exists because the model's prose cannot be unit-tested; this turns "does
it read well and pass the gates" into an artifact a person can read. Three
modes, cheapest first:

    python -m tools.newsroom_ai --print-prompt --story-id N
        Renders the exact system prompt and stdin payload for a persisted story
        and exits. NO auth, NO call, NO cost -- how a prompt change is reviewed.

    python -m tools.newsroom_ai --live --story-id N
        One real `claude` call. Prints the envelope's telemetry, the gate
        results and the text that WOULD be stored. Writes nothing.

    python -m tools.newsroom_ai --live --cycle N --league-id L --apply
        The real pass over one cycle's unposted template stories, by hand,
        outside a press. Updates only the stories that pass the gates.

Run with `-m` from the repo root (see `tools/seed_dev_league.py` for why).
`--live` implies the AI writer is on for this run regardless of
`MEDIA_AI_ENABLED`; every other `MEDIA_AI_*` setting (model, effort, personas,
cap, timeout) is read from the environment as the bot would read it. Live
mode spends subscription usage.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys

from dba2.core.config import get_config
from dba2.data.db import close_pool, get_pool
from dba2.data.repositories import league_repo, media_repo
from dba2.services import media_service


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--print-prompt", action="store_true",
                      help="render the prompt for --story-id; no call, no cost")
    mode.add_argument("--live", action="store_true",
                      help="make real claude CLI calls (spends subscription usage)")
    parser.add_argument("--story-id", type=int, help="a persisted media_stories id")
    parser.add_argument("--cycle", type=int, help="cycle_index for a whole-cycle pass")
    parser.add_argument("--league-id", type=int, help="league for --cycle")
    parser.add_argument("--season", type=int, help="season for --cycle (default: league current)")
    parser.add_argument("--apply", action="store_true",
                        help="with --live --cycle: write the passing stories")
    args = parser.parse_args(argv)
    if args.print_prompt and args.story_id is None:
        parser.error("--print-prompt needs --story-id")
    if args.live and (args.story_id is None) == (args.cycle is None):
        parser.error("--live needs exactly one of --story-id or --cycle")
    if args.cycle is not None and args.league_id is None:
        parser.error("--cycle needs --league-id")
    if args.cycle is not None and not args.apply:
        parser.error("--cycle is the real pass: add --apply (use --story-id to preview one story)")
    if args.apply and args.cycle is None:
        parser.error("--apply only applies to --cycle")
    return args


def _print_preview(preview: media_service.AiStoryPreview, *, live: bool) -> None:
    print("=== SYSTEM PROMPT ===")
    print(preview.system_prompt)
    print("=== STDIN PAYLOAD ===")
    print(preview.payload)
    if not live:
        return
    outcome = preview.outcome
    assert outcome is not None
    print("=== OUTCOME ===")
    if outcome.telemetry is not None:
        print(f"telemetry: {dataclasses.asdict(outcome.telemetry)}")
    if outcome.written is None:
        print(f"FAILED category={outcome.category} subtype={outcome.cli_subtype} "
              f"detail={outcome.detail}")
        return
    gate = preview.gate
    assert gate is not None
    print(f"gates: {'PASS' if gate.passed else f'FAIL {gate.gate} {list(gate.offending)}'}")
    print(f"would-be headline: {preview.would_be_headline}")
    print(f"would-be body:     {preview.would_be_body}")


async def _main(args: argparse.Namespace) -> int:
    config = get_config()
    if args.live:
        config = dataclasses.replace(config, media_ai_enabled=True)
    library = media_service.load_media_library()
    pool = await get_pool()
    try:
        if args.story_id is not None:
            try:
                preview = await media_service.preview_story_for_ai(
                    pool, story_id=args.story_id, library=library, config=config, live=args.live
                )
            except LookupError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            _print_preview(preview, live=args.live)
            return 0
        league = await league_repo.get_league(pool, args.league_id)
        if league is None:
            print(f"no league {args.league_id}", file=sys.stderr)
            return 2
        season = args.season if args.season is not None else league.current_season
        summary = await media_service.rewrite_cycle_stories_with_ai(
            pool, league_id=league.id, season=season, cycle_index=args.cycle,
            library=library, config=config,
        )
        print(dataclasses.asdict(summary))
        for story_id in summary.updated_story_ids:
            story = await media_repo.get_media_story(pool, story_id)
            if story is not None:
                print(f"--- story {story.id} [{story.voice}] {story.headline}")
                print(story.rendered_text)
        return 0
    finally:
        await close_pool()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
