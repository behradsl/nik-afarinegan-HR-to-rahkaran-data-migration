# FormulaContext ↔ source formula mapping summary

## Scope

- Dest: `HCM3.FormulaContext` where `[Key]=EmployeeStatuteCalc`
- Source: `PAY_PayrollBackFormula` / `PAY_PfFormula` for mapped statute factors, plus post-split `OutputExpr` from property migration mapping

## Counts

- Context inventory entries: **868**
- Mapping rows: **49** (mapped=21, eliminated=2, partial=4, leftover=7, expr_shapes=15)
- Distinct non-zero OutputExpr shapes: **15**

## What maps cleanly

- Arithmetic `+ - * / ()`
- `CASE/WHEN` → `if / else if / else`
- `round` → `Round`; `convert(numeric,…)` → `ToDecimal`
- `BFactor{N}_A` / `Factor{N}_A` when N is a **migrated statute factor** → `GetFactorValue` + dynamic `GetStatuteFactorsForStatuteCalc`
- `FxPay_PersonnelKind*` → **eliminated** by ET-specific properties (step 17)

## Main decisions needed (see leftovers.md)

1. **`Self`** — no context token; define base-amount strategy per factor.
2. **`BFactor10_A` (روزهای کارکرد)** and **`BFactor12014_A`** — attendance-like; create/match attendance factors then wire via attendance dynamics.
3. **Unmapped payroll factors** referenced in expressions (140,141,150,217,…) — create as statute factors or exclude from calc.
4. **`FxPay_MonthDayWithPF`** — rare; custom helper vs attendance days.
5. **`BFactor*_TM`** — confirm meaning (month total?) before mapping.

## Observation

Source formulas for these statute factors are **expression-like** (Self, factor refs, arithmetic, CASE, round) — not SQL joins. That matches your read and makes FormulaContext mapping feasible once leftovers above are decided.

## Files

- `context_inventory.csv` — full dest vocabulary extract
- `source_token_inventory.csv` / `source_factor_refs.csv`
- `token_mapping.csv` — full mapping table
- `leftovers.md` — decision list