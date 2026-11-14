"""`presentation/draft.py` -- the draft's lines and its confirmation panel
(docs/design/phase-5-stage-d-prime-draft.md §4 commit 7, §5 T4).

**Goldens, written as literals.** Every expected string below is typed out
by hand rather than rebuilt from the same f-string the renderer uses: a
test that recomputes the answer the way the code does agrees with the
code's own bug, which is the rule commit 3's `(2, 31)` literal was written
under and the same one applies to wording.

**Two properties these tests exist to keep, both negative.** Only the GM
channel's line may contain a mention, because `#games` full of pings is how
a league learns to mute `#games`; and a cycle post must stay inside
Discord's 2000-character message limit, because a message over it is
REJECTED, so a 59-pick cycle with no truncation posts nothing at all rather
than posting too much.
"""

from __future__ import annotations

from dba2.presentation.draft import (
    build_cycle_post,
    build_on_the_clock_line,
    build_on_the_clock_ping,
    build_paused_line,
    build_pick_line,
    build_pick_panel,
    clip_to_message_limit,
)
from dba2.presentation.panel import (
    MAX_DESCRIPTION_CHARS,
    MAX_FIELD_VALUE_CHARS,
    MAX_TITLE_CHARS,
    MAX_TOTAL_CHARS,
    PanelColor,
)

_MAX_MESSAGE_CHARS = 2000


def test_a_pick_line_names_the_round_the_pick_and_who_took_it():
    assert (
        build_pick_line(team_code="TOR", player_name="Alpha Prospect", round=1, pick_number=30)
        == "**Round 1, pick 30:** TOR select Alpha Prospect."
    )


def test_a_managers_own_pick_says_so():
    """The same fact `league_transactions.actor_user_id` carries durably --
    said out loud because a league watching the draft can see who is
    actually turning up for it."""
    assert (
        build_pick_line(
            team_code="TOR",
            player_name="Alpha Prospect",
            round=2,
            pick_number=31,
            by_manager=True,
        )
        == "**Round 2, pick 31:** TOR select Alpha Prospect (manager's pick)."
    )


def test_a_pick_whose_round_is_unknown_is_still_announced():
    """`DraftPickResult` carries a pick number and no round, so a caller
    that cannot resolve one degrades the LINE rather than dropping the
    pick -- pick numbers are unique across the whole draft anyway."""
    assert (
        build_pick_line(team_code="BOS", player_name="Bravo Prospect", round=None, pick_number=31)
        == "**Pick 31:** BOS select Bravo Prospect."
    )


def test_the_public_on_the_clock_line_carries_no_mention():
    line = build_on_the_clock_line(team_code="CHI", round=2, pick_number=32)
    assert line == "**CHI are on the clock** -- round 2, pick 32."
    assert "<@" not in line


def test_only_the_gm_channel_ping_mentions_anybody_and_it_names_the_command():
    ping = build_on_the_clock_ping(manager_user_id=202, team_code="CHI", round=2, pick_number=32)
    assert ping == (
        "<@202> **CHI are on the clock** -- round 2, pick 32. Make the pick with "
        "`/draft code:CHI player_id:<prospect>`."
    )


def test_a_cycle_post_is_one_message_of_lines_then_the_clock():
    body = build_cycle_post(
        [
            "**Round 1, pick 30:** TOR select Alpha Prospect (manager's pick).",
            "**Round 2, pick 31:** BOS select Bravo Prospect.",
        ],
        "**CHI are on the clock** -- round 2, pick 32.",
    )
    assert body == (
        "**Round 1, pick 30:** TOR select Alpha Prospect (manager's pick).\n"
        "**Round 2, pick 31:** BOS select Bravo Prospect.\n"
        "**CHI are on the clock** -- round 2, pick 32."
    )


def test_a_cycle_post_with_nobody_on_the_clock_is_just_its_picks():
    body = build_cycle_post(["**Round 1, pick 1:** TOR select Alpha Prospect."], None)
    assert body == "**Round 1, pick 1:** TOR select Alpha Prospect."


def test_a_paused_draft_says_so_and_says_why():
    """Round-1 critic's M2: a STALLED or SKIPPED cycle used to leave `#games`
    reading like a normal draft that had simply stopped -- every pick line
    posted, no clock line, and nothing saying the draft is not coming back
    on its own. One short line per reason, all ASCII."""
    assert build_paused_line("pool_exhausted") == (
        "**The draft is paused** -- no draft-eligible prospects remain, so the "
        "commissioner needs to add a draft class."
    )
    assert build_paused_line("resume_failed") == (
        "**The draft is paused** -- the AI could not continue (the error is logged), so "
        "ask the commissioner to continue the draft."
    )
    assert "<@" not in build_paused_line("claim_held")
    assert all(ord(c) < 128 for c in build_paused_line("unresolved_picks"))


def test_a_claim_held_line_reports_an_advance_and_never_claims_a_pause():
    """Round-2 critic's minor 5. `SKIPPED(claim_held)` means the
    commissioner is advancing the league RIGHT NOW and the draft continues
    from their press -- so a public line saying the draft is paused is
    false, and the one thing a league would do about it (find the
    commissioner) is exactly what is already happening."""
    line = build_paused_line("claim_held")
    assert line == (
        "**The commissioner is advancing the league** -- the draft continues from there."
    )
    assert "paused" not in line.lower()


def test_no_public_line_tells_a_league_to_run_a_command_it_cannot_run():
    """Round-2 critic's minor 3. `#games` is read by everybody, and after a
    failed or contended resume the next pick usually belongs to an AI team
    -- so "run `/draft` again" is unactionable for almost every reader, and
    for the person who just picked it is wrong outright."""
    for reason in ("resume_failed", "resume_contended", None):
        line = build_paused_line(reason)
        assert "/draft" not in line, line
        assert "ask the commissioner" in line, line


def test_an_unrecognised_pause_reason_still_says_the_draft_is_paused():
    """The reason is a plain string (this module may not import
    `DraftCycleReason` -- it is pure), so a reason added service-side and
    not here must still produce an honest line rather than a KeyError."""
    line = build_paused_line("something_nobody_has_written_yet")
    assert line.startswith("**The draft is paused**")
    assert build_paused_line(None).startswith("**The draft is paused**")


def test_a_paused_line_survives_truncation_like_the_clock_line_does():
    """It is the one sentence that tells a league the draft needs a human,
    so it is in the tail that never gets cut."""
    lines = [f"**Round 2, pick {n}:** TOR select Prospect Numbered{n}." for n in range(30, 90)]
    paused = build_paused_line("pool_exhausted")
    body = build_cycle_post(lines, None, paused_line=paused)

    assert len(body) <= _MAX_MESSAGE_CHARS
    assert body.endswith(paused)
    assert body.startswith("_(earlier picks in this run are not shown)_")


def test_a_long_cycle_keeps_the_clock_line_and_says_what_it_dropped():
    """A 59-pick cycle (every AI team between two people) is real, and
    Discord rejects a message over 2000 characters outright -- without this
    the whole post is lost, including the sentence telling the next person
    to go."""
    lines = [f"**Round 2, pick {n}:** TOR select Prospect Numbered{n}." for n in range(30, 90)]
    clock = "**CHI are on the clock** -- round 2, pick 90."
    body = build_cycle_post(lines, clock)

    assert len(body) <= _MAX_MESSAGE_CHARS
    assert body.endswith(clock)
    assert body.startswith("_(earlier picks in this run are not shown)_")
    assert lines[-1] in body, "the most recent picks are the ones kept"
    assert lines[0] not in body, "nothing was actually dropped, so this proves nothing"


def test_a_cycle_post_that_cannot_fit_even_one_line_still_reports_the_clock():
    clock = "**CHI are on the clock** -- round 2, pick 90."
    body = build_cycle_post(["x" * (_MAX_MESSAGE_CHARS * 2)], clock)
    assert len(body) <= _MAX_MESSAGE_CHARS
    assert body.endswith(clock)


def test_the_pick_panel_carries_the_pick_and_whatever_comes_next():
    panel = build_pick_panel(
        team_code="TOR",
        player_name="Alpha Prospect",
        round=1,
        pick_number=30,
        next_up="CHI are on the clock -- round 2, pick 32.",
    )
    assert panel.title == "TOR select Alpha Prospect"
    assert panel.description == "Round 1, pick 30."
    assert [(f.name, f.value) for f in panel.fields] == [
        ("Next", "CHI are on the clock -- round 2, pick 32.")
    ]
    assert panel.color is PanelColor.POSITIVE
    # `Panel` validates its own limits at construction; this pins that the
    # fixture is nowhere near them rather than passing by luck.
    assert len(panel.title) + len(panel.description) < MAX_TOTAL_CHARS


def test_a_panel_clips_a_name_no_database_column_ever_bounded():
    """Round-1 critic's blocking issue B3, reproduced live with a 300-
    character `last_name`: `players.last_name` is `TEXT`, nothing between
    the row and the embed bounded it, and `Panel.__post_init__` raises on a
    title over 256 characters -- which happened AFTER the pick had
    committed, so the person's pick landed and they got no reply at all.
    The renderer clips; it is the only layer that knows Discord's numbers."""
    panel = build_pick_panel(
        team_code="TOR",
        player_name="Q" * 300,
        round=1,
        pick_number=30,
        next_up="x" * (MAX_FIELD_VALUE_CHARS + 50),
    )
    assert len(panel.title) == MAX_TITLE_CHARS
    assert panel.title.startswith("TOR select QQQ")
    assert [len(f.value) for f in panel.fields] == [MAX_FIELD_VALUE_CHARS]


def test_a_panels_description_is_clipped_too():
    """The description is built from numbers today, so nothing can overflow
    it -- clipped anyway, because the next thing somebody puts in a panel
    description will come from a row, and B3 is what that costs."""
    panel = build_pick_panel(
        team_code="TOR", player_name="Alpha Prospect", round=1, pick_number=30, next_up="Next."
    )
    assert len(panel.description) <= MAX_DESCRIPTION_CHARS


def test_a_message_clip_is_available_to_the_bot_layer():
    """`cogs/draft.py` builds its own reply text (the pick sentence plus
    whatever disclosures it owes) and that text carries a player's name too,
    so it needs the same 2000-character bound this module already applies to
    every line it builds -- from here, rather than a second copy of the
    number in `bot/`."""
    assert clip_to_message_limit("x" * 3000) == "x" * 1999 + "…"
    assert clip_to_message_limit("short") == "short"
