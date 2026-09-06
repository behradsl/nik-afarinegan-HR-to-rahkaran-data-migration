# FormulaContext ↔ source formula mapping

Standalone analysis (not a migration step).

```bash
python analysis/formula_context_mapping/build_context_mapping.py
```

Outputs under `out/`:

| File | Purpose |
|------|---------|
| `mapping_summary.md` | Executive summary |
| `leftovers.md` | Decisions needed |
| `token_mapping.csv` | Full mapping table |
| `context_inventory.csv` | Dest FormulaContext extract |
| `source_token_inventory.csv` / `source_factor_refs.csv` | Source usage |
