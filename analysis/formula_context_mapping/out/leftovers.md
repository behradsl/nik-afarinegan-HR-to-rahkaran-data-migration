# Formula context mapping — leftovers & partials

For decision before implementing C# formula bodies.

## Leftovers (no clean 1:1 context token)

### `Self`
- Category: self
- Notes: No direct FormulaContext token. In source = base amount of the factor being calculated before day-proration. Dest formula usually *computes* that value; needs a design choice (base field, prior factor, or rewrite without Self).
- Evidence: appears in many OutputExpr; top shapes include Self*BFactor10_A/30

### `dbo.FxPay_MonthDayWithPF(...)`
- Category: udf
- Notes: Source UDF returns payable days for person/month/factor. Closest dest helpers are calendar/day-diff or attendance factor APIs, but not 1:1. Likely needs a dedicated attendance/statute helper factor.

### `PAY_PfID_fk`
- Category: parameter
- Notes: Current factor id in source UDF call; dest formula is already scoped to one factor property.

### `BFactor140_A (جذب حقوقي)`
- Category: factor_ref_item
- Notes: Referenced in formulas but not in StatuteFactorMigrationMapping (not on rule-doc detail/score set we migrated). Create as statute factor or attendance/other factor, then map.
- Evidence: occurrences=366; hint=payroll_other

### `BFactor150_A (هيئت تخلفات)`
- Category: factor_ref_item
- Notes: Referenced in formulas but not in StatuteFactorMigrationMapping (not on rule-doc detail/score set we migrated). Create as statute factor or attendance/other factor, then map.
- Evidence: occurrences=366; hint=payroll_other

### `BFactor141_A (غيربومي)`
- Category: factor_ref_item
- Notes: Referenced in formulas but not in StatuteFactorMigrationMapping (not on rule-doc detail/score set we migrated). Create as statute factor or attendance/other factor, then map.
- Evidence: occurrences=366; hint=payroll_other

### `BFactor217_A (استعلاجي)`
- Category: factor_ref_item
- Notes: Referenced in formulas but not in StatuteFactorMigrationMapping (not on rule-doc detail/score set we migrated). Create as statute factor or attendance/other factor, then map.
- Evidence: occurrences=1; hint=payroll_other

## Partials (usable dest API, but not 1:1 / needs confirm)

### `pay_monthid_fk`
- Dest: `GetYearMonth / ApplyDate` (سال/ماه / تاریخ اجرای حکم)
- Notes: Can derive from $EmployeeStatute.ApplyDate via YearMonthHelper; not a raw MonthID param.

### `BFactor{N}_TM`
- Dest: `GetStatuteFactorValue` (مقدار قبلی عامل)
- Notes: Likely month-total / alternate series of factor N. Confirm with payroll expert; may map to GetStatuteFactorValue or a separate factor.

### `BFactor10_A (روزهاي کارکرد)`
- Dest: `GetAttendanceMonthlyFactorsForOtherCalcs / GetSumOfAttendanceFactorInRangeForOtherCalcs` (عوامل کارکرد)
- Notes: Not a statute factor in our map. Conceptually attendance/work-days; map via Attendance FormulaContext dynamics after creating/matching an attendance factor. Until then: leftover for decision.
- Evidence: occurrences=10633; hint=attendance_days

### `BFactor12014_A (روزهاي کسر سختي کار)`
- Dest: `GetAttendanceMonthlyFactorsForOtherCalcs / GetSumOfAttendanceFactorInRangeForOtherCalcs` (عوامل کارکرد)
- Notes: Not a statute factor in our map. Conceptually attendance/work-days; map via Attendance FormulaContext dynamics after creating/matching an attendance factor. Until then: leftover for decision.
- Evidence: occurrences=208; hint=attendance_deduction

## Suggested factors to create / import (from leftover factor refs)

| Source PfID | Name | Why |
|-------------|------|-----|
| BFactor10_A (روزهاي کارکرد) | — | partial: Not a statute factor in our map. Conceptually attendance/work-days; map via Attendance FormulaContext dynamics after cre |
| BFactor140_A (جذب حقوقي) | — | leftover: Referenced in formulas but not in StatuteFactorMigrationMapping (not on rule-doc detail/score set we migrated). Create a |
| BFactor150_A (هيئت تخلفات) | — | leftover: Referenced in formulas but not in StatuteFactorMigrationMapping (not on rule-doc detail/score set we migrated). Create a |
| BFactor141_A (غيربومي) | — | leftover: Referenced in formulas but not in StatuteFactorMigrationMapping (not on rule-doc detail/score set we migrated). Create a |
| BFactor12014_A (روزهاي کسر سختي کار) | — | partial: Not a statute factor in our map. Conceptually attendance/work-days; map via Attendance FormulaContext dynamics after cre |
| BFactor217_A (استعلاجي) | — | leftover: Referenced in formulas but not in StatuteFactorMigrationMapping (not on rule-doc detail/score set we migrated). Create a |