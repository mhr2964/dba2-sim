"""`services/phrase_service.py` -- §11 step 15's own verify list
(docs/design/phase-3-inter-manager-society.md §6.1-§6.3), followed
bullet-for-bullet: a real, valid library loads; each of the five raise
conditions (Decision P3-18's vocabulary check, Decision P3-19's fallback
coverage, Decision P3-31's `exclusive_mass_share` floor, and this
dispatch's own `open`/`stance` scope extension) has its own deliberately-
corrupt fixture; `render()` interpolates correctly.
"""
from __future__ import annotations

import copy
import json

import pytest

from dba2.domain.team import GmPhilosophy
from dba2.managers.voice.frame import SituationToken
from dba2.managers.voice.select import PhraseSelection
from dba2.services.phrase_service import (
    DEFAULT_PHRASE_LIBRARY_PATH,
    load_library,
    render,
)


def _minimal_valid_payload() -> dict:
    """Exactly one no-trigger, all-philosophy fallback per situation token
    (Decision P3-19), one exclusive template per `GmPhilosophy` (weighted
    to clear B4's real per-philosophy exclusive-mass-share floor against
    the ten fallbacks' own wildcard weight -- `10 / (10 + 10) = 0.5 >=
    0.25`), plus the two required top-level knobs -- the smallest payload
    `load_library` accepts."""
    templates = [
        {
            "id": f"fallback_{token.value}",
            "philosophies": [], "situations": [token.value], "windows": [],
            "triggers": [], "weight": 1.0, "text": f"line for {token.value}", "slots": [],
        }
        for token in SituationToken
    ]
    templates += [
        {
            "id": f"exclusive_{philosophy.value}",
            "philosophies": [philosophy.value], "situations": ["open"], "windows": [],
            "triggers": [], "weight": 10.0, "text": f"line for {philosophy.value}", "slots": [],
        }
        for philosophy in GmPhilosophy
    ]
    return {"repeat_penalty": 0.25, "exclusive_mass_share": 0.25, "templates": templates}


def _write(tmp_path, payload: dict, name: str = "lib.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# A real, valid library loads.
# ---------------------------------------------------------------------------


def test_the_real_shipped_library_loads_successfully():
    library = load_library()
    assert library.repeat_penalty == 0.25
    assert library.exclusive_mass_share >= 0.25
    assert len(library.templates) > 0


def test_default_path_points_at_seeds_gm_phrases_json():
    assert DEFAULT_PHRASE_LIBRARY_PATH.name == "gm_phrases.json"
    assert DEFAULT_PHRASE_LIBRARY_PATH.parent.name == "seeds"


def test_minimal_valid_payload_loads(tmp_path):
    path = _write(tmp_path, _minimal_valid_payload())
    library = load_library(path)
    assert len(library.templates) == len(list(SituationToken)) + len(list(GmPhilosophy))


# ---------------------------------------------------------------------------
# Decision P3-18 -- unknown feature/operator raises at load.
# ---------------------------------------------------------------------------


def test_raises_on_unknown_trigger_feature(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"].append(
        {
            "id": "bad_feature", "philosophies": [], "situations": ["open"], "windows": [],
            "triggers": [{"feature": "not_a_real_field", "op": "gte", "value": 0.5}],
            "weight": 1.0, "text": "x", "slots": [],
        }
    )
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="not_a_real_field"):
        load_library(path)


def test_raises_on_unknown_trigger_operator(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"].append(
        {
            "id": "bad_op", "philosophies": [], "situations": ["open"], "windows": [],
            "triggers": [{"feature": "top_in_share", "op": "between", "value": 0.5}],
            "weight": 1.0, "text": "x", "slots": [],
        }
    )
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="between"):
        load_library(path)


# ---------------------------------------------------------------------------
# Decision P3-19 -- exactly one no-trigger, all-philosophy fallback per
# situation token.
# ---------------------------------------------------------------------------


def test_raises_when_a_situation_token_has_no_fallback(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"] = [
        t for t in payload["templates"] if t["id"] != "fallback_open"
    ]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="open"):
        load_library(path)


def test_raises_when_a_situation_token_has_two_fallbacks(tmp_path):
    payload = _minimal_valid_payload()
    duplicate = copy.deepcopy(payload["templates"][0])
    duplicate["id"] = duplicate["id"] + "_duplicate"
    payload["templates"].append(duplicate)
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="more than one fallback"):
        load_library(path)


def test_a_wildcard_philosophy_fallback_with_a_trigger_does_not_count_as_the_fallback(tmp_path):
    """A fallback must have `triggers == ()` -- a triggered template,
    however broad its `philosophies`/`situations`, never satisfies
    Decision P3-19's coverage requirement on its own."""
    payload = _minimal_valid_payload()
    payload["templates"] = [
        t for t in payload["templates"] if t["id"] != "fallback_open"
    ]
    payload["templates"].append(
        {
            "id": "triggered_not_a_fallback", "philosophies": [], "situations": ["open"],
            "windows": [], "triggers": [{"feature": "round_index", "op": "gte", "value": 0}],
            "weight": 1.0, "text": "x", "slots": [],
        }
    )
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="open"):
        load_library(path)


def test_a_window_scoped_fallback_shaped_template_does_not_count_as_the_fallback(tmp_path):
    """B5 -- a no-trigger, no-philosophy template with a non-empty
    `windows` list is NOT a true fallback: `select.py::select_phrase`
    filters on `windows` too, so a mismatched-window turn would leave zero
    survivors and crash at selection time (`IndexError`/`ValueError` from
    `rng.choices`) if this template were accepted as the situation's
    designated fallback. Load must raise instead of silently accepting it."""
    payload = _minimal_valid_payload()
    payload["templates"] = [
        t for t in payload["templates"] if t["id"] != "fallback_open"
    ]
    payload["templates"].append(
        {
            "id": "window_scoped_not_a_real_fallback", "philosophies": [], "situations": ["open"],
            "windows": ["contend"], "triggers": [],
            "weight": 1.0, "text": "x", "slots": [],
        }
    )
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="open"):
        load_library(path)


# ---------------------------------------------------------------------------
# Decision P3-31 -- exclusive_mass_share's load-time floor.
# ---------------------------------------------------------------------------


def test_raises_when_exclusive_mass_share_is_below_the_floor(tmp_path):
    payload = _minimal_valid_payload()
    payload["exclusive_mass_share"] = 0.1
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="0.25"):
        load_library(path)


def test_raises_when_exclusive_mass_share_is_missing(tmp_path):
    """Nit N6, §11 step 15's follow-up round: a missing key now raises a
    friendly, path-naming `ValueError` (matching `repeat_penalty`'s own
    N1 fix) rather than a bare `KeyError`."""
    payload = _minimal_valid_payload()
    del payload["exclusive_mass_share"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="exclusive_mass_share"):
        load_library(path)


def test_raises_when_exclusive_mass_share_is_not_a_number(tmp_path):
    payload = _minimal_valid_payload()
    payload["exclusive_mass_share"] = "a lot"
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="number"):
        load_library(path)


def test_exclusive_mass_share_exactly_at_the_floor_is_accepted(tmp_path):
    payload = _minimal_valid_payload()
    payload["exclusive_mass_share"] = 0.25
    path = _write(tmp_path, payload)
    library = load_library(path)
    assert library.exclusive_mass_share == 0.25


# ---------------------------------------------------------------------------
# Critic finding B4 -- the REAL per-philosophy exclusive-mass-share check,
# not just the declared-number floor above.
# ---------------------------------------------------------------------------


def test_raises_when_a_philosophy_has_zero_exclusive_templates(tmp_path):
    """A deliberately-corrupt fixture proving `_validate_exclusive_mass_
    share_by_philosophy` actually raises: one philosophy (`chaos`) has no
    exclusive template at all, so its ratio is `0.0 / selectable_weight <
    0.25` regardless of how high the declared top-level number is -- the
    OLD validator (checking only the declared number) would have accepted
    this file outright."""
    payload = _minimal_valid_payload()
    payload["templates"] = [
        t for t in payload["templates"] if t["id"] != "exclusive_chaos"
    ]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="chaos"):
        load_library(path)


def test_raises_when_a_philosophys_exclusive_weight_share_is_below_the_declared_floor(tmp_path):
    """Same defect, milder shape -- `chaos` HAS an exclusive template, but
    its weight is too small relative to the wildcard mass it competes
    against to clear the declared `0.25` floor."""
    payload = _minimal_valid_payload()
    for template in payload["templates"]:
        if template["id"] == "exclusive_chaos":
            template["weight"] = 0.5  # 0.5 / (10.0 wildcard + 0.5) < 0.25
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="chaos"):
        load_library(path)


def test_a_library_where_every_philosophy_clears_the_real_floor_loads(tmp_path):
    """The positive case -- `_minimal_valid_payload`'s own construction
    (one exclusive template per philosophy, weighted to clear the floor
    against the fallbacks' wildcard mass) must load cleanly; this is the
    same assertion `test_minimal_valid_payload_loads` makes, restated here
    next to B4's own corrupt-fixture tests for locality."""
    path = _write(tmp_path, _minimal_valid_payload())
    library = load_library(path)
    assert library.exclusive_mass_share == 0.25


def test_raises_when_a_multi_philosophy_template_would_be_dropped_from_the_denominator(
    tmp_path,
):
    """Re-verification finding B-1: `selectable_weight(P)` must count a
    template selectable via MEMBERSHIP (`P.value in t.philosophies`, the
    same test `select.py::_template_matches_dimensions` applies), not just
    a wildcard or an exact-tuple match on `P` alone. A prior version of
    this validator used `t.philosophies == (philosophy.value,)` on BOTH
    the exclusive and the selectable side, which silently excluded any
    multi-philosophy template (`philosophies: ["star_maxer",
    "egalitarian"]`, schema-legal per `select.py`'s own docstring) from
    every philosophy's denominator -- shrinking `selectable_weight` and
    inflating `ratio` in exactly the direction that lets a bad library
    pass. Reproduced directly: a `star_maxer`-and-`egalitarian`-shared
    template at weight 1000, alongside `_minimal_valid_payload`'s own
    weight-10 fallbacks/exclusives, must NOT let `star_maxer`'s real
    ratio (`10 / (10 + 10 + 1000) = 0.0098`) pass as if it were `10 / 20 =
    0.5` under the old, wrong denominator."""
    payload = _minimal_valid_payload()
    payload["templates"].append(
        {
            "id": "shared_star_egalitarian",
            "philosophies": ["star_maxer", "egalitarian"],
            "situations": ["open"], "windows": [], "triggers": [],
            "weight": 1000.0, "text": "shared line", "slots": [],
        }
    )
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="star_maxer"):
        load_library(path)


# ---------------------------------------------------------------------------
# This dispatch's own scope extension to Decision P3-18 -- no `open`
# template may trigger on `stance`.
# ---------------------------------------------------------------------------


def test_raises_when_an_open_situation_template_triggers_on_stance(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"].append(
        {
            "id": "open_bad_stance_trigger", "philosophies": [], "situations": ["open"],
            "windows": [], "triggers": [{"feature": "stance", "op": "eq", "value": "counter"}],
            "weight": 1.0, "text": "x", "slots": [],
        }
    )
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="stance"):
        load_library(path)


def test_raises_when_a_wildcard_situations_template_triggers_on_stance(tmp_path):
    """A template with `situations == ()` (wildcard) implicitly applies to
    `open` too -- the rule must catch this case, not just an explicit
    `"open"` entry in `situations`."""
    payload = _minimal_valid_payload()
    payload["templates"].append(
        {
            "id": "wildcard_bad_stance_trigger", "philosophies": [], "situations": [],
            "windows": [], "triggers": [{"feature": "stance", "op": "eq", "value": "counter"}],
            "weight": 1.0, "text": "x", "slots": [],
        }
    )
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="stance"):
        load_library(path)


def test_a_non_open_template_may_trigger_on_stance(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"].append(
        {
            "id": "counter_ask_more_stance_trigger", "philosophies": [],
            "situations": ["counter_ask_more"], "windows": [],
            "triggers": [{"feature": "stance", "op": "eq", "value": "counter"}],
            "weight": 1.0, "text": "x", "slots": [],
        }
    )
    path = _write(tmp_path, payload)
    library = load_library(path)
    assert any(t.id == "counter_ask_more_stance_trigger" for t in library.templates)


# ---------------------------------------------------------------------------
# Nit N1 -- repeat_penalty's load-time validation.
# ---------------------------------------------------------------------------


def test_raises_with_a_clear_error_when_repeat_penalty_is_missing(tmp_path):
    payload = _minimal_valid_payload()
    del payload["repeat_penalty"]
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="repeat_penalty"):
        load_library(path)


def test_raises_when_repeat_penalty_is_at_or_above_one(tmp_path):
    """`repeat_penalty >= 1.0` would turn repeated template use into a
    BONUS instead of a penalty (`select.py`'s own `weight * (repeat_penalty
    ** prior_uses)` formula) -- previously accepted silently."""
    payload = _minimal_valid_payload()
    payload["repeat_penalty"] = 5.0
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="repeat_penalty"):
        load_library(path)


def test_raises_when_repeat_penalty_is_negative(tmp_path):
    payload = _minimal_valid_payload()
    payload["repeat_penalty"] = -0.1
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="repeat_penalty"):
        load_library(path)


# ---------------------------------------------------------------------------
# Nit N7 -- weight's load-time validation.
# ---------------------------------------------------------------------------


def test_raises_when_a_weight_is_zero(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"][0]["weight"] = 0.0
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="weight"):
        load_library(path)


def test_raises_when_a_weight_is_negative(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"][0]["weight"] = -1.0
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="weight"):
        load_library(path)


# ---------------------------------------------------------------------------
# Duplicate template ids.
# ---------------------------------------------------------------------------


def test_raises_on_duplicate_template_id(tmp_path):
    payload = _minimal_valid_payload()
    duplicate = copy.deepcopy(payload["templates"][0])
    payload["templates"].append(duplicate)
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="duplicate"):
        load_library(path)


# ---------------------------------------------------------------------------
# render() -- Decision P3-16's interpolation step.
# ---------------------------------------------------------------------------


def test_render_interpolates_slot_bindings(tmp_path):
    payload = _minimal_valid_payload()
    payload["templates"].append(
        {
            "id": "with_slots", "philosophies": ["star_maxer"], "situations": ["counter_ask_more"],
            "windows": [], "triggers": [{"feature": "top_in_share", "op": "gte", "value": 0.75}],
            "weight": 1.0, "text": "Put {ask_asset} in and we can talk.", "slots": ["ask_asset"],
        }
    )
    path = _write(tmp_path, payload)
    library = load_library(path)

    selection = PhraseSelection(template_id="with_slots", slot_bindings={"ask_asset": "the pick"})
    assert render(selection, library) == "Put the pick in and we can talk."


def test_render_with_no_slots_needed_passes_through():
    library = load_library()
    fallback = next(t for t in library.templates if not t.triggers and not t.philosophies)
    selection = PhraseSelection(template_id=fallback.id, slot_bindings={})
    assert render(selection, library) == fallback.text


# ---------------------------------------------------------------------------
# B6 item 1 -- at least one triggered, non-fallback template per illegal
# token, on the real shipped library. Verified against the five literal
# strings `trade.py::_check_legality` actually emits (`trade.py:511, 540,
# 543, 545, 549`), not trusted from any report's own citation of them.
# ---------------------------------------------------------------------------

_REAL_ILLEGAL_TOKENS = ("roster_max", "roster_min", "apron_2", "apron_1", "salary_matching")


@pytest.mark.parametrize("illegal_token", _REAL_ILLEGAL_TOKENS)
def test_real_library_has_a_triggered_template_per_real_illegal_token(illegal_token):
    library = load_library()
    matching = [
        t for t in library.templates
        if any(tr.feature == "illegal_token" and tr.op == "eq" and tr.value == illegal_token
               for tr in t.triggers)
    ]
    assert matching, f"no non-fallback template triggers on illegal_token == {illegal_token!r}"
