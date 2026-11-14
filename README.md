# DBA 2

A simulated NBA league that runs as a Discord bot. It has a possession-level game sim, rule-driven AI general managers that handle contracts, trades, free agency and the draft, a media and newsroom layer that writes stories from league events, and a phase-based season state machine.

## Layout

| Package | What it holds |
| --- | --- |
| `dba2/sim` | Possession and game simulation, box scores, tuning constants |
| `dba2/managers`, `dba2/agency` | GM decision logic and player motivation/agency |
| `dba2/media` | Story detection, salience, framing |
| `dba2/domain` | Plain domain types shared across layers |
| `dba2/services` | Orchestration across domain, sim and data |
| `dba2/presentation` | Discord embeds, broadcasts, panels |
| `dba2/phase` | Season phase state machine and command gates |
| `dba2/data` | asyncpg repositories (PostgreSQL) |
| `dba2/bot` | discord.py client and cogs |
| `tools/` | GM validation harness and corpus tools |
| `seeds/` | Static seed data (real-player ratings, phrase banks, cap settings) |

## Running the tests

Requires Python 3.12.

```sh
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.lock.txt
pip install -e . --no-deps
pytest tests/unit
```

`pytest tests/unit` needs no database, Discord token or network.

- Tests marked `slow` (multi-season harness runs, large seeded corpora) are excluded by default. Run them with `pytest tests/unit -m slow`.
- `tests/integration` needs a live PostgreSQL (`docker compose up -d db`, then `DATABASE_URL` from `.env.example`, then `alembic upgrade head`).

## License

MIT. See [LICENSE](LICENSE).
