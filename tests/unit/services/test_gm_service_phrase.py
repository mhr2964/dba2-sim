"""§11 step 15 -- dialogue wired into the live negotiation loop
(docs/design/phase-3-inter-manager-society.md §6.3, this dispatch's own
architect round). Three things this file owns, per step 15's own verify
clause:

1. `_outcome_phrase_speaker_id`'s three-branch ladder, hand-built (Tier 1,
   no corpus needed).
2. The RNG-salt correction (situation token appended) actually
   differentiates same-speaker/same-round draws.
3. Real-corpus obligations: dialogue on/off produces byte-identical
   `DecisionRecord`s (Decision P3-20), the `reject_illegal` speaker
   invariant (`illegal_token is not None`), and the `open` structural
   invariant (`open_selections == negotiations - illegal_round0_
   terminations`).

`phrase_service.py`'s own load-time validator is tested separately in
`test_phrase_service.py` -- this file trusts `load_library()` and only
exercises the wiring.
"""
from __future__ import annotations

import pytest

from dba2.core.rng import rng_for
from dba2.domain.phase import Phase
from dba2.domain.team import GmPhilosophy
from dba2.managers import negotiation
from dba2.managers.explain import DecisionRecord
from dba2.managers.tuning import DEFAULT_TUNING, CompetitiveWindow
from dba2.managers.voice.frame import SituationToken
from dba2.services import gm_service
from dba2.services.gm_service import NegotiationSituation
from dba2.services.phrase_service import load_library
from tools.gm_harness.league import generate_gm_league
from tools.gm_harness.season import run_seed
from tools.gm_harness.state import build_ctx, build_snapshot

_LEAGUE_ID = 1
_SEASON = 2026
_SEED = 1


def _record(
    team_id: int, *, verdict: str = "accept",
    philosophy: GmPhilosophy = GmPhilosophy.STAR_MAXER,
    window: CompetitiveWindow = CompetitiveWindow.CONTEND,
) -> DecisionRecord:
    return DecisionRecord(
        team_id=team_id, philosophy=philosophy, window=window, subject="p1<->p2",
        verdict=verdict, score=0.0, threshold=0.0, terms=(),
    )


# ---------------------------------------------------------------------------
# _outcome_phrase_speaker_id -- Tier 1, three branches, hand-built.
# ---------------------------------------------------------------------------


def test_reject_illegal_branch_prefers_record_a_when_a_is_the_illegal_one():
    speaker = gm_service._outcome_phrase_speaker_id(
        situation=NegotiationSituation.REJECT_ILLEGAL,
        record_a=_record(1, verdict="illegal:apron_1"), record_b=_record(2, verdict="reject"),
        a_id=1, b_id=2, actor_id=1, short_side_id=None,
    )
    assert speaker == 1


def test_reject_illegal_branch_picks_b_when_only_b_is_illegal():
    speaker = gm_service._outcome_phrase_speaker_id(
        situation=NegotiationSituation.REJECT_ILLEGAL,
        record_a=_record(1, verdict="reject"), record_b=_record(2, verdict="illegal:roster_max"),
        a_id=1, b_id=2, actor_id=2, short_side_id=None,
    )
    assert speaker == 2


def test_reject_illegal_branch_checks_record_a_first_when_both_are_illegal():
    """Not a documented production shape (both sides illegal from the same
    package is possible in principle -- independent per-side legality
    checks), but the ladder's own stated rule is unconditional: `record_a`
    is checked first, regardless of `record_b`."""
    speaker = gm_service._outcome_phrase_speaker_id(
        situation=NegotiationSituation.REJECT_ILLEGAL,
        record_a=_record(1, verdict="illegal:apron_2"),
        record_b=_record(2, verdict="illegal:salary_matching"),
        a_id=1, b_id=2, actor_id=1, short_side_id=None,
    )
    assert speaker == 1


@pytest.mark.parametrize(
    "situation",
    [
        NegotiationSituation.COUNTER_GIVE_LESS,
        NegotiationSituation.COUNTER_ASK_MORE,
        NegotiationSituation.REJECT_TOO_FAR,
        NegotiationSituation.EXPIRE_STALLED,
        NegotiationSituation.EXPIRE_ROUNDS,
    ],
)
def test_short_side_branch_wins_whenever_short_side_id_is_set(situation):
    """§4.2 cell 3's population -- the short side speaks, regardless of
    who the actor was this turn (the actor and the short side coincide on
    a real give-less turn but are independent inputs to this function)."""
    speaker = gm_service._outcome_phrase_speaker_id(
        situation=situation, record_a=_record(1), record_b=_record(2),
        a_id=1, b_id=2, actor_id=2, short_side_id=1,
    )
    assert speaker == 1

    speaker_b_short = gm_service._outcome_phrase_speaker_id(
        situation=situation, record_a=_record(1), record_b=_record(2),
        a_id=1, b_id=2, actor_id=1, short_side_id=2,
    )
    assert speaker_b_short == 2


@pytest.mark.parametrize(
    "situation",
    [
        NegotiationSituation.ACCEPT_ROUND0,
        NegotiationSituation.ACCEPT_AFTER_COUNTER,
        NegotiationSituation.REJECT_DEADLOCK,
    ],
)
def test_default_branch_returns_the_non_actor_when_short_side_id_is_none(situation):
    speaker_when_a_acted = gm_service._outcome_phrase_speaker_id(
        situation=situation, record_a=_record(1), record_b=_record(2),
        a_id=1, b_id=2, actor_id=1, short_side_id=None,
    )
    assert speaker_when_a_acted == 2

    speaker_when_b_acted = gm_service._outcome_phrase_speaker_id(
        situation=situation, record_a=_record(1), record_b=_record(2),
        a_id=1, b_id=2, actor_id=2, short_side_id=None,
    )
    assert speaker_when_b_acted == 1


# ---------------------------------------------------------------------------
# The RNG-salt correction -- the appended situation-token component.
# ---------------------------------------------------------------------------


def test_phrase_rng_salt_differs_by_situation_token():
    """This dispatch's own correction to §6.3 step 6: without the
    situation-token component, the SAME speaker's `open` and outcome
    selections in the SAME round (the modal give-less round-0 path) would
    derive an IDENTICAL stream from Decision P3-20's original 7-tuple.
    Appending the situation token (as a plain string) as the salt's 8th
    component is what `_select_negotiation_phrase` actually does -- this
    confirms the mechanism at the `rng_for` level directly."""
    common = (1, "phrase", 10, 2026, Phase.REGULAR_SEASON, "p1<->p2", 0)
    open_rng = rng_for(*common, str(SituationToken.OPEN))
    outcome_rng = rng_for(*common, str(SituationToken.COUNTER_GIVE_LESS))
    assert open_rng.random() != outcome_rng.random()


def test_situation_token_stringifies_to_its_bare_value():
    # The salt component must be the bare token string ("open"), not an
    # enum repr -- confirmed directly since `rng_for` treats any two
    # distinct strings as distinct salts regardless of representation, but
    # the design doc's own text specifically states the plain-string form.
    assert str(SituationToken.OPEN) == "open"
    assert str(SituationToken.REJECT_ILLEGAL) == "reject_illegal"


# ---------------------------------------------------------------------------
# Real-corpus obligations -- byte-identity, the two structural invariants.
# ---------------------------------------------------------------------------


def _capture_negotiations(monkeypatch, snapshot, ctx, phrase_library):
    """Wraps `gm_service._run_negotiation` for the scope of one `run_
    trade_window` call, returning every `Negotiation` it produced --
    mirrors `_count_real_evaluations`'s own module-attribute-patching
    precedent (`gm_service.py`) for getting an exact, ordered accounting
    without changing `run_trade_window`'s own return contract."""
    captured: list[gm_service.Negotiation] = []
    original = gm_service._run_negotiation

    def _spy(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result

    # `monkeypatch.context()`, not the shared fixture directly -- this
    # function may be called more than once per test (see the "run twice"
    # determinism test below), and the shared fixture only undoes patches
    # at test teardown. Without a scoped context, a second call's own
    # `original` would capture the FIRST call's still-installed spy rather
    # than the true `_run_negotiation`, double-recording into both lists.
    with monkeypatch.context() as m:
        m.setattr(gm_service, "_run_negotiation", _spy)
        gm_service.run_trade_window(snapshot, ctx, phrase_library=phrase_library)
    return captured


def _phrase_selection_signature(negotiations):
    """A stable, comparable summary of every phrase this run's
    negotiations chose -- `(opening template_id or None, [outcome
    template_id per turn])` per negotiation, in `run_trade_window`'s own
    (deterministic) iteration order."""
    return tuple(
        (
            n.opening_phrase.template_id if n.opening_phrase is not None else None,
            tuple(t.phrase.template_id if t.phrase is not None else None for t in n.turns),
        )
        for n in negotiations
    )


@pytest.fixture(scope="module")
def _real_corpus():
    """One real, deterministic 30-team league, one `REGULAR_SEASON` trade
    window's worth of negotiations -- built once per module (`scope=
    "module"`), read by every test below."""
    library = load_library()
    state = generate_gm_league(
        league_id=_LEAGUE_ID, season=_SEASON, rng_seed=_SEED, players_per_team=15,
        tuning=DEFAULT_TUNING,
    )
    snapshot = build_snapshot(state)
    ctx = build_ctx(state, phase=Phase.REGULAR_SEASON, tuning=DEFAULT_TUNING)
    return snapshot, ctx, library


@pytest.mark.slow
def test_dialogue_on_produces_byte_identical_decision_records_to_dialogue_off():
    """Decision P3-20 + P3-29's digest precedent, restated as this step's
    own verify clause: "with dialogue on, every `DecisionRecord` in a
    fixed-seed run is byte-identical to step 13's." A full `run_seed` call
    (1 season, `skip_games=True`) run twice -- once with `phrase_library=
    None`, once with a real loaded library -- must produce byte-identical
    `SeasonRecord.jsonl_lines` (the same encoded-`DecisionRecord` artifact
    the committed control digest compares)."""
    kwargs = dict(
        league_id=_LEAGUE_ID, rng_seed=_SEED, first_season=_SEASON, seasons=1,
        players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING,
    )
    record_off = run_seed(**kwargs)
    record_on = run_seed(**kwargs, phrase_library=load_library())

    assert record_off.seasons[0].jsonl_lines == record_on.seasons[0].jsonl_lines


@pytest.mark.slow
def test_reject_illegal_selections_always_have_a_populated_illegal_token(monkeypatch, _real_corpus):
    """Decision P3-15's invariant, asserted the way this dispatch's own
    brief requires: every `reject_illegal` OUTCOME selection's frame has
    `illegal_token is not None` -- picking the wrong side would produce a
    `reject_illegal` frame with `illegal_token=None`, which can never
    match a triggered per-`illegal_token` template.

    **Nit N4 -- strengthened to assert on the PRODUCTION-PATH frame
    directly, not a freshly re-derived one.** The old version re-derived a
    SEPARATE frame with `head_to_head=None, p_bar=None`, which could never
    catch a bug in `_select_negotiation_phrase`'s own `(speaker_id,
    listener_id)` h2h lookup key -- a swapped key would still re-derive
    fine here, since this test would simply pass `None` regardless. This
    version spies on `gm_service.derive_frame` itself (mirroring `dump_
    frames`-style instrumentation used elsewhere in this file) to capture
    the REAL `head_to_head` row the production call passed in, and asserts
    it IS (identity, not just equality) the row `ctx.head_to_head` maps the
    real `(speaker_id, listener_id)` pair to -- a wrong key would fetch a
    different object (or `None` where a real row exists) and fail this
    identity check. (This fixture's own `ctx.head_to_head` happens to be
    empty on these first-ever negotiations, so every row is `None` either
    way here -- the check is real and would catch a swap the moment this
    file's fixture carries populated memory, which N3's own regression
    test, immediately below, exercises for the shading channel.)"""
    snapshot, ctx, library = _real_corpus

    captured_negotiations: list[gm_service.Negotiation] = []
    captured_illegal_frames: list[tuple[int, object, object]] = []
    original_run_negotiation = gm_service._run_negotiation
    original_derive_frame = gm_service.derive_frame

    def _negotiation_spy(*args, **kwargs):
        result = original_run_negotiation(*args, **kwargs)
        captured_negotiations.append(result)
        return result

    def _frame_spy(record, *, stance, situation, round_index, head_to_head, p_bar):
        frame = original_derive_frame(
            record, stance=stance, situation=situation, round_index=round_index,
            head_to_head=head_to_head, p_bar=p_bar,
        )
        if situation == SituationToken.REJECT_ILLEGAL:
            captured_illegal_frames.append((record.team_id, head_to_head, frame))
        return frame

    with monkeypatch.context() as m:
        m.setattr(gm_service, "_run_negotiation", _negotiation_spy)
        m.setattr(gm_service, "derive_frame", _frame_spy)
        gm_service.run_trade_window(snapshot, ctx, phrase_library=library)

    illegal_turns = [
        turn for negotiation in captured_negotiations for turn in negotiation.turns
        if turn.situation == NegotiationSituation.REJECT_ILLEGAL
    ]
    assert illegal_turns, "fixture error -- this corpus must contain real illegal terminations"
    assert len(captured_illegal_frames) == len(illegal_turns), (
        "exactly one production-path derive_frame(situation=reject_illegal) call must occur "
        "per real reject_illegal turn"
    )

    for turn, (speaker_team_id, head_to_head_row, frame) in zip(
        illegal_turns, captured_illegal_frames, strict=True
    ):
        assert turn.phrase is not None
        assert speaker_team_id == turn.phrase_speaker_id
        assert frame.illegal_token is not None

        listener_team_id = (
            turn.record_b.team_id if speaker_team_id == turn.record_a.team_id
            else turn.record_a.team_id
        )
        expected_row = (
            ctx.head_to_head.get((speaker_team_id, listener_team_id))
            if ctx.head_to_head is not None else None
        )
        assert head_to_head_row is expected_row


@pytest.mark.slow
def test_open_selection_count_equals_negotiations_minus_illegal_round0_terminations(
    monkeypatch, _real_corpus
):
    """The structural invariant this dispatch's own brief names directly:
    `open_selections == negotiations - illegal_round0_terminations`,
    exactly -- one `open` line per negotiation, suppressed only when round
    0 itself terminates `REJECT_ILLEGAL`."""
    snapshot, ctx, library = _real_corpus
    negotiations = _capture_negotiations(monkeypatch, snapshot, ctx, library)

    open_count = sum(1 for n in negotiations if n.opening_phrase is not None)
    illegal_round0 = sum(
        1 for n in negotiations
        if n.turns[0].situation == NegotiationSituation.REJECT_ILLEGAL
    )

    assert open_count > 0
    assert illegal_round0 > 0
    assert open_count == len(negotiations) - illegal_round0


@pytest.mark.slow
def test_every_turn_gets_exactly_one_outcome_phrase_when_dialogue_is_on(monkeypatch, _real_corpus):
    snapshot, ctx, library = _real_corpus
    negotiations = _capture_negotiations(monkeypatch, snapshot, ctx, library)
    for neg in negotiations:
        for turn in neg.turns:
            assert turn.phrase is not None
            assert turn.phrase_speaker_id in (neg.initiator_id, neg.responder_id)


@pytest.mark.slow
def test_dialogue_off_leaves_every_phrase_field_none(monkeypatch, _real_corpus):
    snapshot, ctx, _library = _real_corpus
    negotiations = _capture_negotiations(monkeypatch, snapshot, ctx, phrase_library=None)
    assert negotiations  # fixture sanity: real negotiations happened
    for neg in negotiations:
        assert neg.opening_phrase is None
        for turn in neg.turns:
            assert turn.phrase is None
            assert turn.phrase_speaker_id is None


@pytest.mark.slow
def test_running_the_same_trade_window_twice_produces_identical_phrase_selections(
    monkeypatch, _real_corpus
):
    """`phrase_use_counts` lives inside `_run_negotiation`, freshly
    instantiated per call (§11 step 15's own correction: per-negotiation
    scope, never per-window or module-level) -- calling `run_trade_window`
    twice against byte-identical `(snapshot, ctx, phrase_library)` must
    select byte-identical phrases both times. A `phrase_use_counts` that
    leaked across negotiations or across calls (e.g. an accidental
    module-level dict) would down-weight the second run's early template
    choices relative to the first.

    **Nit N2 -- this test alone only proves no leakage that survives past
    a whole `run_trade_window` CALL** (a dict scoped per-window rather than
    per-negotiation would ALSO pass this test: both full-window replays
    start from the same empty window-level dict and accumulate identically
    in the same negotiation order, so they would still match). It does
    NOT prove isolation BETWEEN negotiations within a single call. See
    `test_a_negotiations_own_phrase_selections_do_not_depend_on_earlier_
    negotiations_in_the_same_window`, below, for the test that actually
    proves the narrower, real claim this docstring used to (incorrectly)
    claim for itself."""
    snapshot, ctx, library = _real_corpus
    negotiations_1 = _capture_negotiations(monkeypatch, snapshot, ctx, library)
    negotiations_2 = _capture_negotiations(monkeypatch, snapshot, ctx, library)

    assert _phrase_selection_signature(negotiations_1) == _phrase_selection_signature(
        negotiations_2
    )


@pytest.mark.slow
def test_a_negotiations_own_phrase_selections_do_not_depend_on_earlier_negotiations_in_same_window(
    monkeypatch, _real_corpus
):
    """**Nit N2's real fix.** Captures the exact `(args, kwargs)` a REAL,
    LATE negotiation in a live `run_trade_window` pass was called with
    (one that ran after several earlier negotiations already populated
    whatever state those earlier calls left behind), then replays
    `_run_negotiation` on that SAME captured call in total isolation --
    fresh `tally`/`h2h` accumulators, no prior calls in this process at
    all -- and asserts the replay's phrase selections are BYTE-IDENTICAL
    to what that negotiation produced live, in situ.

    This is the test the previous test's docstring incorrectly claimed to
    already be: if `phrase_use_counts` were scoped per-WINDOW rather than
    per-negotiation (or leaked via any other shared mutable state), the
    late negotiation's LIVE result would carry down-weighting from
    everything that ran before it in the same window, while the isolated
    REPLAY -- called completely fresh, as its own standalone `_run_
    negotiation` invocation -- would not, and the two would diverge. A
    correctly per-negotiation-scoped `phrase_use_counts` (fresh dict,
    every call, regardless of how many negotiations already ran) makes
    the replay and the live call identical by construction."""
    snapshot, ctx, library = _real_corpus

    captured_calls = []
    original = gm_service._run_negotiation

    def _spy(*args, **kwargs):
        result = original(*args, **kwargs)
        captured_calls.append((args, kwargs, result))
        return result

    with monkeypatch.context() as m:
        m.setattr(gm_service, "_run_negotiation", _spy)
        gm_service.run_trade_window(snapshot, ctx, phrase_library=library)

    assert len(captured_calls) >= 5, "fixture error -- need several negotiations to pick a late one"
    late_args, late_kwargs, live_result = captured_calls[len(captured_calls) // 2]

    replay_kwargs = dict(late_kwargs)
    replay_kwargs["tally"] = gm_service._NegotiationTally()
    replay_kwargs["h2h"] = gm_service._H2HAccumulator(league_id=ctx.league_id, season=ctx.season)

    replay_result = original(*late_args, **replay_kwargs)

    assert _phrase_selection_signature([replay_result]) == _phrase_selection_signature(
        [live_result]
    )


# ---------------------------------------------------------------------------
# B6 items 2 and 3 -- real-corpus obligations step 15's own verify clause
# names explicitly but never turned into an assertion.
# ---------------------------------------------------------------------------


def _all_turns(negotiations):
    return [turn for negotiation in negotiations for turn in negotiation.turns]


@pytest.mark.slow
def test_reject_illegal_fallback_share_stays_within_its_own_documented_bound(
    monkeypatch, _real_corpus
):
    """B6 item 2 -- `reject_illegal`'s own fallback rate, measured
    SEPARATELY from the pooled M26 figure (step 15's own verify clause:
    "reject_illegal's own fallback rate is checked separately... since
    that one situation alone can consume most of this budget"). A fresh
    measurement on the real library against this fixture's own corpus
    (30-team league, one `REGULAR_SEASON` window) landed at **0.0352**
    (26/738) -- re-verification's own independent re-measurement of this
    exact fixture, not the 0.00 an earlier draft of this docstring
    claimed (that number never reproduced; corrected here rather than
    trusted). The bound asserted here (`<= 0.10`) sits with real margin
    above the measured 0.0352 -- enough that ordinary corpus variance
    across seeds/scales shouldn't make this flaky -- while staying well
    under M26's own pooled `0.20` cap (`seeds/gm_validation_bands.json`),
    which is the budget this situation alone must not consume."""
    snapshot, ctx, library = _real_corpus
    negotiations = _capture_negotiations(monkeypatch, snapshot, ctx, library)
    illegal_turns = [
        t for t in _all_turns(negotiations) if t.situation == NegotiationSituation.REJECT_ILLEGAL
    ]
    assert illegal_turns, "fixture error -- this corpus must contain real illegal terminations"
    fallback_count = sum(1 for t in illegal_turns if t.phrase.template_id.startswith("fallback_"))
    share = fallback_count / len(illegal_turns)
    assert share <= 0.10, f"reject_illegal's own fallback share ({share:.4f}) exceeded its bound"


@pytest.mark.slow
def test_reject_too_far_split_is_reachable_on_both_sides_of_the_real_gap_over_band_cutoff(
    monkeypatch, _real_corpus
):
    """B6 item 3 -- now that `gap_over_band` (B2) makes the `reject_too_far`
    split reachable, at least one triggered template must exist on EACH
    side of the split in the real library, AND both sides must actually
    get SELECTED at least once in a real corpus (library presence alone
    would not catch a template that is present but unreachable -- e.g. a
    trigger with an impossible value)."""
    snapshot, ctx, library = _real_corpus

    far_side_ids = {
        t.id for t in library.templates
        if any(
            tr.feature == "gap_over_band" and tr.op == "lt" and tr.value <= -1.0
            for tr in t.triggers
        )
    }
    in_band_side_ids = {
        t.id for t in library.templates
        if any(
            tr.feature == "gap_over_band" and tr.op == "gte" and tr.value >= -1.0
            for tr in t.triggers
        )
    }
    assert far_side_ids, "the library has no triggered template on the far side of the split"
    assert in_band_side_ids, "the library has no triggered template on the in-band side"

    negotiations = _capture_negotiations(monkeypatch, snapshot, ctx, library)
    reject_too_far_turns = [
        t for t in _all_turns(negotiations)
        if t.situation == NegotiationSituation.REJECT_TOO_FAR
    ]
    assert reject_too_far_turns, "fixture error -- this corpus must contain real far rejections"

    selected_ids = {t.phrase.template_id for t in reject_too_far_turns}
    assert selected_ids & far_side_ids, "no far-side template was ever selected in this corpus"
    assert selected_ids & in_band_side_ids, "no in-band-side template was ever selected"


# ---------------------------------------------------------------------------
# Nit N3 -- dialogue must read the real, unshaded `record_a`/`record_b`,
# never channel 3's synthetic/shaded stand-in (`_substitute_shaded_record`'s
# return value, `respond_record_a`/`respond_record_b` in `_run_negotiation`'s
# own local scope). This module's own dialogue-block comment and `_select_
# negotiation_phrase`'s docstring already state this as a rule; this test
# is the regression guard that would catch a future violation of it.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_dialogue_reads_the_real_record_never_channel_3s_shaded_stand_in():
    """Runs a full season (both `REGULAR_SEASON` and `TRADE_DEADLINE`
    trade windows, so `TRADE_DEADLINE` negotiations carry real accumulated
    head-to-head memory and channel 3's shading is genuinely active for at
    least some pairs -- unlike `_real_corpus`'s single first-ever window,
    where every pair's memory is empty and shading can never fire), then
    proves the "dialogue never reads the shaded stand-in" rule
    STRUCTURALLY, by identity: `_substitute_shaded_record` returns a
    `dataclasses.replace(...)` copy -- a NEW object, never sharing identity
    with the real `actor_record` -- so if `_select_negotiation_phrase` ever
    read the shaded object instead of the turn's own real `record_a`/
    `record_b`, the `record is turn.record_a or record is turn.record_b`
    check below would fail for at least one shaded turn. Byte-value
    comparison would not catch this as reliably: `_substitute_shaded_
    record` rescales `threshold`/`score` but deliberately leaves `gap =
    score - threshold` UNCHANGED (its own docstring), so a real vs. shaded
    record's `gap_abs` can coincide by construction even when the wrong
    object was used -- identity is the only check that cannot pass by
    accident."""
    library = load_library()

    respond_calls: list[tuple[DecisionRecord, DecisionRecord]] = []
    original_respond = negotiation.respond

    def _respond_spy(record_a, record_b, **kwargs):
        respond_calls.append((record_a, record_b))
        return original_respond(record_a, record_b, **kwargs)

    negotiations: list[gm_service.Negotiation] = []
    original_run_negotiation = gm_service._run_negotiation

    def _negotiation_spy(*args, **kwargs):
        result = original_run_negotiation(*args, **kwargs)
        negotiations.append(result)
        return result

    outcome_frame_calls: list[tuple[DecisionRecord, object]] = []
    original_derive_frame = gm_service.derive_frame

    def _frame_spy(record, *, situation, **kwargs):
        frame = original_derive_frame(record, situation=situation, **kwargs)
        if situation != SituationToken.OPEN:
            outcome_frame_calls.append((record, frame))
        return frame

    with pytest.MonkeyPatch.context() as m:
        m.setattr(gm_service.negotiation, "respond", _respond_spy)
        m.setattr(gm_service, "_run_negotiation", _negotiation_spy)
        m.setattr(gm_service, "derive_frame", _frame_spy)
        run_seed(
            league_id=_LEAGUE_ID, rng_seed=_SEED, first_season=_SEASON, seasons=1,
            players_per_team=15, skip_games=True, tuning=DEFAULT_TUNING,
            phrase_library=library,
        )

    all_turns = [turn for negotiation_ in negotiations for turn in negotiation_.turns]
    assert len(respond_calls) == len(all_turns), "one respond() call must occur per real turn"
    assert len(outcome_frame_calls) == len(all_turns), (
        "one outcome derive_frame() call must occur per real turn"
    )

    shaded_turns_seen = 0
    for turn, (respond_record_a, respond_record_b), (frame_record, _frame) in zip(
        all_turns, respond_calls, outcome_frame_calls, strict=True
    ):
        shaded_a = respond_record_a is not turn.record_a
        shaded_b = respond_record_b is not turn.record_b
        if shaded_a or shaded_b:
            shaded_turns_seen += 1
        # The outcome frame's own record must be one of THIS turn's two
        # real records -- never the shaded stand-in, whether or not this
        # particular turn happened to be shaded.
        assert frame_record is turn.record_a or frame_record is turn.record_b

    assert shaded_turns_seen > 0, (
        "fixture error -- this run must exercise channel 3's shading at least once, or this "
        "test is not exercising the code path it claims to guard"
    )
