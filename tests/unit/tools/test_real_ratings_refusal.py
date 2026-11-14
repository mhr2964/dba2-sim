"""RPD-8 wire-in: every real-player loader refuses a season that is not ratings version 2
(`require_builder_version`) unless `--allow-legacy-ratings`, and the builder owns peak ages.

Runs on the committed `seeds/real_players`: 2009-2025 are version 2, 1996-2008 are the
untouched legacy files (no raw evidence exists for them). No database is touched."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest

from tools import (
    _gm_real_player_peak_age as peak_age,
)
from tools import (
    _real_fields as rf,
)
from tools import (
    box_score_real_matchup as box,
)
from tools import (
    matchup_server,
    seed_dev_league,
)

_LEGACY_SEASON = 2000
_REBUILT_SEASON = 2025


def _seed_rows(season: int) -> list[dict]:
    path = seed_dev_league._REAL_PLAYERS_DIR / f"{season}.json"
    return json.loads(path.read_text(encoding="utf-8"))["players"]


# ------------------------------------------------------------ the seeds themselves


def test_seasons_2009_to_2025_are_version_two_and_1996_to_2008_are_legacy() -> None:
    for season in range(2009, 2026):
        rf.require_builder_version(_seed_rows(season), season, allow_legacy=False)
    for season in range(1996, 2009):
        with pytest.raises(rf.LegacyRatingsError, match=f"season {season}:"):
            rf.require_builder_version(_seed_rows(season), season, allow_legacy=False)


def test_refusal_message_names_the_season_and_a_rebuild_command_that_can_run() -> None:
    with pytest.raises(rf.LegacyRatingsError) as exc:
        rf.require_builder_version(_seed_rows(_LEGACY_SEASON), _LEGACY_SEASON, False)
    message = str(exc.value)
    assert f"season {_LEGACY_SEASON}" in message
    assert f"_gm_real_player_fetch_raw {_LEGACY_SEASON} {_LEGACY_SEASON}" in message
    # a partial build cannot write seeds/real_players, so the command must be --all
    assert "_gm_real_player_build --all --out-dir seeds/real_players --allow-seeds-dir" in message
    assert "--allow-legacy-ratings" in message


# ------------------------------------------------------------------ seed_dev_league


def test_seed_dev_league_loader_refuses_legacy_and_accepts_with_the_flag() -> None:
    with pytest.raises(rf.LegacyRatingsError, match=f"season {_LEGACY_SEASON}"):
        seed_dev_league._load_real_player_pool(_LEGACY_SEASON)
    legacy = seed_dev_league._load_real_player_pool(_LEGACY_SEASON, allow_legacy_ratings=True)
    assert legacy
    assert all((r.get("meta") or {}).get("builder_version") is None for r in legacy)
    rebuilt = seed_dev_league._load_real_player_pool(_REBUILT_SEASON)
    assert {r["meta"]["builder_version"] for r in rebuilt} == {2}


def _fail_if_the_database_is_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_pool():
        raise AssertionError("the database must not be reached before the refusal")

    monkeypatch.setattr(seed_dev_league, "get_pool", get_pool)


@pytest.mark.parametrize("replace_league", [False, True])
def test_seed_dev_league_refuses_a_legacy_season_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch, replace_league: bool
) -> None:
    """`--replace-league` deletes the guild's old league first, so a refusal that came
    after it would destroy a league and then fail."""
    _fail_if_the_database_is_touched(monkeypatch)
    with pytest.raises(rf.LegacyRatingsError, match=f"season {_LEGACY_SEASON}"):
        asyncio.run(
            seed_dev_league.seed_dev_league(
                discord_guild_id=1,
                name="x",
                start_season_year=2026,
                current_season=2026,
                commissioner_user_id=1,
                rng_seed=1,
                real_players_season=_LEGACY_SEASON,
                real_players_per_team=14,
                replace_league=replace_league,
            )
        )


def test_seed_dev_league_with_the_flag_gets_past_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReachedTheDatabase(Exception):
        pass

    async def get_pool():
        raise ReachedTheDatabase

    monkeypatch.setattr(seed_dev_league, "get_pool", get_pool)
    with pytest.raises(ReachedTheDatabase):
        asyncio.run(
            seed_dev_league.seed_dev_league(
                discord_guild_id=1,
                name="x",
                start_season_year=2026,
                current_season=2026,
                commissioner_user_id=1,
                rng_seed=1,
                real_players_season=_LEGACY_SEASON,
                real_players_per_team=14,
                allow_legacy_ratings=True,
            )
        )


def test_seed_dev_league_cli_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["seed_dev_league"])
    assert seed_dev_league._parse_args().allow_legacy_ratings is False
    monkeypatch.setattr(sys, "argv", ["seed_dev_league", "--allow-legacy-ratings"])
    assert seed_dev_league._parse_args().allow_legacy_ratings is True


# ------------------------------------------------------------ box_score_real_matchup


def test_box_score_loader_refuses_legacy_and_accepts_with_the_flag() -> None:
    with pytest.raises(rf.LegacyRatingsError, match=f"season {_LEGACY_SEASON}"):
        box._load_real_player_pool(_LEGACY_SEASON)
    assert box._load_real_player_pool(_LEGACY_SEASON, allow_legacy_ratings=True)
    assert box._load_real_player_pool(_REBUILT_SEASON)


@pytest.mark.parametrize(("extra", "expected"), [([], False), (["--allow-legacy-ratings"], True)])
def test_box_score_cli_passes_the_flag_to_the_loader(
    monkeypatch: pytest.MonkeyPatch, tmp_path, extra: list[str], expected: bool
) -> None:
    seen: dict = {}

    class Stop(Exception):
        pass

    def loader(season: int, *, allow_legacy_ratings: bool = False):
        seen.update(season=season, allow=allow_legacy_ratings)
        raise Stop

    monkeypatch.setattr(box, "_load_real_player_pool", loader)
    monkeypatch.setattr(
        sys, "argv", ["box", "--season", "2000", "--out", str(tmp_path / "o.html"), *extra]
    )
    with pytest.raises(Stop):
        box.main()
    assert seen == {"season": 2000, "allow": expected}


def test_box_score_cli_default_refuses_a_legacy_season(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(sys, "argv", ["box", "--season", "2000", "--out", str(tmp_path / "o.html")])
    with pytest.raises(rf.LegacyRatingsError, match="season 2000"):
        box.main()


# ------------------------------------------------------------------- matchup_server


@pytest.fixture
def fresh_matchup_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(matchup_server, "_POOL_CACHE", {})
    monkeypatch.setattr(matchup_server, "_CATALOG", {})
    monkeypatch.setattr(matchup_server, "_ALLOW_LEGACY_RATINGS", False)


def test_importing_matchup_server_builds_no_catalog() -> None:
    """The catalog used to be built at import, which would now raise for 1996-2008."""
    result = subprocess.run(
        [sys.executable, "-c", "import tools.matchup_server as m; print(len(m._CATALOG))"],
        capture_output=True,
        text=True,
        cwd=seed_dev_league._REPO_ROOT,
        check=True,
    )
    assert result.stdout.strip() == "0"


def test_matchup_catalog_leaves_legacy_seasons_out_and_says_so(fresh_matchup_state) -> None:
    catalog, refused = matchup_server._build_catalog()
    assert {entry["season"] for entry in catalog.values()} == set(range(2009, 2026))
    assert len(refused) == 13
    for season, message in zip(range(1996, 2009), refused, strict=True):
        assert f"season {season}:" in message
        assert "_gm_real_player_build --all" in message
    assert set(matchup_server._POOL_CACHE) == set(range(2009, 2026))


def test_matchup_catalog_with_the_flag_offers_every_season(fresh_matchup_state) -> None:
    catalog, refused = matchup_server._build_catalog(allow_legacy_ratings=True)
    assert refused == []
    assert {entry["season"] for entry in catalog.values()} == set(range(1996, 2026))


def test_matchup_pool_for_season_refuses_legacy_unless_the_flag_is_set(
    monkeypatch: pytest.MonkeyPatch, fresh_matchup_state
) -> None:
    with pytest.raises(rf.LegacyRatingsError, match=f"season {_LEGACY_SEASON}"):
        matchup_server._pool_for_season(_LEGACY_SEASON)
    monkeypatch.setattr(matchup_server, "_ALLOW_LEGACY_RATINGS", True)
    assert matchup_server._pool_for_season(_LEGACY_SEASON)


class _StopServer:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def shutdown(self) -> None:
        pass


@pytest.mark.parametrize(
    ("extra", "expected_seasons"),
    [([], set(range(2009, 2026))), (["--allow-legacy-ratings"], set(range(1996, 2026)))],
)
def test_matchup_server_cli_flag_controls_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
    fresh_matchup_state,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    expected_seasons: set[int],
) -> None:
    monkeypatch.setattr(matchup_server, "ThreadingHTTPServer", _StopServer)
    monkeypatch.setattr(matchup_server, "_INDEX_HTML", None)
    monkeypatch.setattr(sys, "argv", ["matchup_server", "--no-browser", *extra])
    matchup_server.main()
    assert {e["season"] for e in matchup_server._CATALOG.values()} == expected_seasons
    stderr = capsys.readouterr().err
    if extra:
        assert "left out of the picker" not in stderr
    else:
        assert "13 season(s) left out of the picker" in stderr
        assert "season 1996:" in stderr


# ----------------------------------------------------------------- peak-age tool


def test_peak_age_tool_refuses_a_directory_holding_version_two_seasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed = json.loads((seed_dev_league._REAL_PLAYERS_DIR / "2023.json").read_text("utf-8"))
    seed["players"] = seed["players"][:5]
    path = tmp_path / "2023.json"
    path.write_text(json.dumps(seed, indent=2), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(peak_age, "_SEEDS_DIR", tmp_path)

    assert peak_age.main() == 1
    assert path.read_bytes() == before
    assert "the RPD-8 builder owns" in capsys.readouterr().err


def test_peak_age_tool_still_runs_on_a_purely_legacy_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    seed = json.loads((seed_dev_league._REAL_PLAYERS_DIR / "2000.json").read_text("utf-8"))
    seed["players"] = seed["players"][:5]
    path = tmp_path / "2000.json"
    path.write_text(json.dumps(seed, indent=2), encoding="utf-8")
    monkeypatch.setattr(peak_age, "_SEEDS_DIR", tmp_path)

    assert peak_age.main() == 0
    rows = json.loads(path.read_text(encoding="utf-8"))["players"]
    assert all(r["ratings"]["peak_age_start"] <= r["ratings"]["peak_age_end"] for r in rows)


# ------------------------------------------------- real-team rosters on rebuilt seeds


@pytest.mark.parametrize("season", [2025, 2024, 2023])
def test_real_team_rosters_find_fourteen_real_players_for_all_thirty_teams(season: int) -> None:
    teams = json.loads((seed_dev_league._REPO_ROOT / "seeds" / "teams.json").read_text("utf-8"))
    codes = [t["code"] for t in teams["teams"]]
    assert len(codes) == 30
    placed = seed_dev_league._real_players_on_their_real_teams(
        seed_dev_league._load_real_player_pool(season), team_codes=codes, per_team=14
    )
    assert sorted(placed) == list(range(30))
    refs = [row["external_ref"] for rows in placed.values() for row in rows]
    assert all(len(rows) == 14 for rows in placed.values())
    assert len(refs) == len(set(refs)) == 30 * 14
