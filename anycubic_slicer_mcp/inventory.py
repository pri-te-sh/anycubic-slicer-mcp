"""A small local spool inventory: what's on the shelf, how much is left, and whether a print fits."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .install import state_dir


class Inventory:
    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "spools.json"

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, spools: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(spools, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, material: str, total_g: float = 1000, remaining_g: float | None = None, brand: str | None = None,
            colour_name: str | None = None, colour_hex: str | None = None, filament_preset: str | None = None,
            price: float | None = None, notes: str | None = None) -> dict[str, Any]:
        if total_g <= 0 or total_g > 20000:
            raise ValueError("total_g must be between 0 and 20000")
        spool = {
            "id": uuid.uuid4().hex[:6], "material": material, "brand": brand, "colour_name": colour_name,
            "colour_hex": colour_hex, "filament_preset": filament_preset, "total_g": float(total_g),
            "remaining_g": float(total_g if remaining_g is None else remaining_g), "price": price,
            "notes": notes, "added": time.strftime("%Y-%m-%d"),
        }
        spools = self._load()
        spools.append(spool)
        self._save(spools)
        return spool

    def list(self, need_g: float | None = None, material: str | None = None) -> dict[str, Any]:
        spools = self._load()
        if material:
            spools = [s for s in spools if material.lower() in str(s.get("material", "")).lower()]
        result: dict[str, Any] = {"spools": spools, "count": len(spools)}
        if need_g is not None:
            result["enough_for_print"] = [s["id"] for s in spools if s.get("remaining_g", 0) >= need_g * 1.05]
            result["note"] = "Spools with at least the needed grams plus a 5% margin."
        return result

    def update(self, spool_id: str, remaining_g: float | None = None, use_g: float | None = None,
               remove: bool = False, **fields: Any) -> dict[str, Any]:
        spools = self._load()
        for i, s in enumerate(spools):
            if s["id"] == spool_id:
                if remove:
                    spools.pop(i)
                    self._save(spools)
                    return {"removed": spool_id}
                if remaining_g is not None:
                    s["remaining_g"] = max(0.0, float(remaining_g))
                if use_g is not None:
                    s["remaining_g"] = max(0.0, s.get("remaining_g", 0) - float(use_g))
                for k, v in fields.items():
                    if v is not None and k in {"brand", "colour_name", "colour_hex", "filament_preset", "price", "notes", "material"}:
                        s[k] = v
                self._save(spools)
                return s
        raise KeyError(f"No spool with id '{spool_id}'. Use list_spools to see ids.")
