"""Persistencia de ejecuciones para auditoría: JSON completo + índice SQLite consultable."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from src.models import RunRecord

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, created_at TEXT, mode TEXT, assets TEXT, overall_risk TEXT,
    regime TEXT, total_extra_eur REAL, report_path TEXT, json_path TEXT, errors INTEGER
);
CREATE TABLE IF NOT EXISTS decisions (
    run_id TEXT, created_at TEXT, asset_id TEXT, opportunity_score INTEGER, conviction TEXT,
    analyst_action TEXT, supervisor_action TEXT, final_action TEXT, amount_eur REAL,
    PRIMARY KEY (run_id, asset_id)
);
"""


class RunStore:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = runs_dir / "history.sqlite"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def save(self, record: RunRecord) -> Path:
        """Guarda el JSON (siempre primero) y actualiza el índice SQLite."""
        json_path = self.runs_dir / f"run_{record.run_id}.json"
        json_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.run_id, record.created_at, record.mode, ",".join(record.assets_requested),
                        record.supervisor.overall_risk if record.supervisor else None,
                        record.macro.regime if record.macro else None,
                        record.total_extra_eur, record.report_path, str(json_path), len(record.errors),
                    ),
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (record.run_id, record.created_at, d.asset_id, d.opportunity_score, d.conviction,
                         d.analyst_action, d.supervisor_action, d.final_action, d.amount_eur)
                        for d in record.decisions
                    ],
                )
        except sqlite3.Error as exc:
            logger.error("No se pudo actualizar el índice SQLite: %s (el JSON sí se guardó)", exc)
        return json_path

    @staticmethod
    def load(path: Path) -> RunRecord:
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def history(self, limit: int = 20) -> list[tuple]:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT created_at, asset_id, opportunity_score, final_action, amount_eur, run_id "
                "FROM decisions ORDER BY created_at DESC, asset_id LIMIT ?",
                (limit,),
            ).fetchall()
