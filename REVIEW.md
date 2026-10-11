# REVIEW.md

## Defects identified and fixed

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes()` | The shift start was calculated using the punch-in's local calendar date. This gives incorrect lateness calculations for overnight shifts when the attendance date differs from the local date. | Create an overnight employee with a 22:00 shift start and punch in after midnight. Verify lateness against the previous day's shift start. | Added an optional attendance date parameter and calculate the shift start using that date. |
| 2 | `compute_derived()` | The lateness calculation did not explicitly use the attendance record's date when calculating derived fields. | Regularize an overnight attendance record and compare the recalculated `late_minutes` with the expected value. | Pass the record's attendance date to `compute_late_minutes()`. |
| 3 | `punch_in()` | The lateness calculation did not explicitly use the resolved attendance date for overnight shifts. | Punch in after midnight during an overnight shift and verify both the attendance date and lateness. | Pass the resolved attendance date to the lateness helper. |
| 4 | `department_trend()` | The original pipeline contained overlapping gap-filling and projection logic, making the daily series difficult to reason about and risking incorrect output. | Request a range containing dates without attendance records and check for exactly one row per calendar day. | Simplified the pipeline so MongoDB generates the daily series, calculates headcount and attendance rates, and computes the moving average. |
| 5 | `department_trend()` — `$densify` | The upper bound did not reliably include the requested end date when the end date had no records. | Request a date range whose final day has no attendance records and verify that the final day is returned. | Use an exclusive upper bound of the day after the requested end date. |
| 6 | `department_trend()` — moving average | The moving average must use the current day and up to the previous six daily rows, including gap-filled days. | Test a range with missing weekdays and weekends and compare the moving average with the preceding six rows. | Use MongoDB's `$setWindowFields` with a `documents: [-6, 0]` window. |
| 7 | `/admin/explain/employee_monthly` | The explain pipeline was simplified and calculated only total late minutes, unlike the actual monthly analytics pipeline. | Compare the explain pipeline with the monthly endpoint's aggregation stages. | Replaced the simplified pipeline with the full monthly aggregation pipeline. |
| 8 | `/admin/explain/department_summary` | The explain pipeline used a basic employee-to-attendance lookup without the actual date, join-date, and aggregation logic. | Compare the explain pipeline with the department summary endpoint, including employees without attendance logs. | Replaced the simplified pipeline with the complete department summary pipeline. |
| 9 | `/admin/explain/late_leaderboard` | The explain pipeline omitted important stages, including employee lookup, department filtering, ranking, and the final projection. | Compare the explain pipeline with the actual leaderboard for tied employees and department-filtered results. | Included the full leaderboard aggregation, including competition ranking and the post-ranking limit. |
| 10 | `/admin/explain/department_trend` | The explain pipeline only matched employees and looked up attendance logs. It did not explain the actual daily trend calculations. | Compare the explain pipeline with the department trend endpoint. | Included the complete trend pipeline, including gap filling, headcount, attendance rates, and the moving average. |
| 11 | `create_indexes()` | The employee indexes did not include a standalone index on `joined_on` for summary queries without a department filter. | Inspect the execution plan for a department summary that includes all departments and filters employees by their joining date. | Added an index on `joined_on` to support this query pattern. |
| 12 | `safe_explain()` / `/admin/explain/{endpoint}` | The explain helper existed but was not consistently used to execute explain commands. | Inspect the explain endpoint and check whether the helper is used to build the explain request. | Used the helper for the attendance-list query and retained execution statistics for the explain response. |

## Items reviewed and considered correct

- The unique index on `attendance_logs` using `(emp_code, date)` prevents duplicate attendance records for the same employee and date.
- Duplicate-key errors during concurrent punch-ins are converted to HTTP 409 responses.
- Punch-out uses a conditional update so concurrent requests cannot both successfully punch out the same record.
- Regularization uses a conditional update and appends history, preventing silent overwrites when concurrent updates conflict.
- Timestamp inputs are validated as integer epoch milliseconds, checked against the supported range, and truncated to whole seconds.
- Department summary starts from employees and preserves employees without attendance logs, allowing headcount to include eligible employees with no logs.
- The leaderboard uses MongoDB's `$rank` to support competition ranking for ties.
- Analytics calculations are performed using MongoDB aggregation pipelines rather than fetching all attendance records and calculating the results in Python.
