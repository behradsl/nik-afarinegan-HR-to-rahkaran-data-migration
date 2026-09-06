# Statute-factor formula break report (date × employment type)

Standalone analysis + shared helpers used by **step 17**.

- Core logic: `utils/formula_break.py`
- Report runner: `analyze_formula_breaks.py` in this folder

## Run report

```bash
python analysis/formula_et_date_break/analyze_formula_breaks.py
```

## Step 17

`steps/step_17_statute_factor.py` uses the same break/expand helpers and inserts
`StatuteFactorProperty` per `(factor, EffectiveFromMonth, DestEmploymentTypeID)`,
with stub C# bodies and `OutputExpr` stored in `Formula.Description`.
