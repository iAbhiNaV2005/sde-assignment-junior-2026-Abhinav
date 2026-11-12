"""Atomic writer for the final_state.csv output contract."""
from __future__ import annotations

import csv
import logging
import os
import tempfile
from typing import Any

from .config import AppConfig
from .store import StateStore

logger = logging.getLogger("dispatcher.output")

FIELDNAMES = [
    "campaign_id",
    "lead_id",
    "final_state",
    "attempts",
    "last_status",
    "last_disposition",
]


def write_final_state_csv(store: StateStore, config: AppConfig, output_path: str) -> None:
    """Atomically writes final_state.csv sorted by campaign_id, lead_id."""
    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)

    rows = store.get_final_states(config)
    # Sort strictly by (campaign_id, lead_id)
    sorted_rows = sorted(rows, key=lambda r: (r["campaign_id"], r["lead_id"]))

    fd, tmp_file = tempfile.mkstemp(dir=output_dir, prefix="final_state_", suffix=".tmp")
    try:
        with open(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for r in sorted_rows:
                writer.writerow({
                    "campaign_id": r["campaign_id"],
                    "lead_id": r["lead_id"],
                    "final_state": r["final_state"],
                    "attempts": r["attempts"],
                    "last_status": r["last_status"] or "",
                    "last_disposition": r["last_disposition"] or "",
                })
        # Atomic rename/replace
        os.replace(tmp_file, output_path)
        logger.info("Successfully wrote %d lead records to %s", len(sorted_rows), output_path)
    except Exception:
        if os.path.exists(tmp_file):
            os.remove(tmp_file)
        raise

