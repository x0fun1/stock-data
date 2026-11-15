#!/usr/bin/env python
"""Convert retained Parquet news datasets once, before removing the old environment.

Run this script with the old pandas/pyarrow environment. These libraries are
migration-only and are not dependencies of the news skill. Without
--remove-legacy the original Parquet evidence is retained.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from stock_research.storage.catalog import Catalog
from stock_research.storage.curated_store import write_record_file


def migrate_runtime(root: Path, remove_legacy: bool = False) -> dict:
    root = root.resolve()
    catalog = Catalog(root)
    converted = []
    for name in ("news", "macro", "securities"):
        for entry in catalog.list_datasets(name):
            original = Path(entry["path"])
            if original.suffix != ".parquet":
                continue
            if not original.resolve().is_relative_to(root):
                raise ValueError("legacy dataset leaves the runtime root")
            # Import only when a legacy dataset actually needs conversion.
            import pandas as pd
            from stock_research.control.ingest import _jsonish

            frame = pd.read_parquet(original)
            records = json.loads(frame.to_json(orient="records", date_format="iso", double_precision=15))
            # Earlier versions serialized nested fields inside Parquet columns.
            for row in records:
                for field, default in (("security_ids", []), ("permissions", {})):
                    if field in row:
                        row[field] = _jsonish(row[field], default)
            destination = original.with_suffix(".json.gz")
            write_record_file(destination, records)
            with gzip.open(destination, "rt", encoding="utf-8") as handle:
                if json.load(handle) != records:
                    raise ValueError("migration verification failed")
            catalog.replace_dataset_path(name, entry["version"], destination)
            converted.append({"dataset": name, "version": entry["version"], "rows": len(records)})
            if remove_legacy:
                original.unlink()
    return {"converted": converted, "versions": len(converted), "originals_removed": remove_legacy}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--remove-legacy", action="store_true", help="delete each original only after conversion is verified")
    args = parser.parse_args()
    print(json.dumps(migrate_runtime(args.root, args.remove_legacy), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
