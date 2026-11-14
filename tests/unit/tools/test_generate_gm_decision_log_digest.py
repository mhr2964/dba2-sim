"""`tools/generate_gm_decision_log_digest.py` -- critic-round B2 gap: zero
test coverage existed on this module at all before this file, so nothing
would fail if a future refactor silently dropped one of `_run_configured_
seed`'s explicit `run_seed` pins (`max_negotiation_rounds=0, head_to_head_
enabled=False, agency_enabled=False, media_enabled=False`) -- exactly the
"self-judged, no artifact" gap this project has been burned by before
(Decision P3-48 needed a mid-build correction for the same shape).

Deliberately avoids a real `run_seed` call (costs real wall-clock time even
under `--skip-games`) -- `run_seed` is monkeypatched to this module's own
imported name, matching `tests/unit/tools/test_validate_gm.py`'s own
established pattern for the identical call site's sibling."""
from __future__ import annotations

import tools.generate_gm_decision_log_digest as digest_module
from tools.gm_harness.season import RunRecord


def _fake_run_seed(**kwargs) -> RunRecord:
    return RunRecord(
        rng_seed=kwargs["rng_seed"], philosophy_by_team={},
        season1_top1_minus_8th_by_team={}, seasons=(),
    )


def test_run_configured_seed_pins_all_four_control_flags(monkeypatch):
    """Decision P3-29(b)/P3-48/P4-55's full pin set, asserted together so a
    future refactor that drops any ONE of the four fails this test
    immediately, rather than silently reproducing a different run than the
    one committed to `seeds/gm_decision_log_digest.json`."""
    calls: list[dict] = []

    def _spy_run_seed(**kwargs):
        calls.append(kwargs)
        return _fake_run_seed(**kwargs)

    monkeypatch.setattr(digest_module, "run_seed", _spy_run_seed)
    digest_module._run_configured_seed(
        rng_seed=1, league_id=1, season_year=2026, seasons=2, players_per_team=15,
    )
    assert len(calls) == 1
    call = calls[0]
    assert call.get("max_negotiation_rounds") == 0
    assert call.get("head_to_head_enabled") is False
    assert call.get("agency_enabled") is False, (
        "the digest generator must pin agency_enabled=False -- see Decision P4-55"
    )
    assert call.get("media_enabled") is False, (
        "the digest generator must pin media_enabled=False -- see Decision P4-55"
    )
