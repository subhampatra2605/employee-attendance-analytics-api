# REVIEW.md

## Defects found in the starter

| # | Where | What is wrong | How to notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `health()` | Returns success without checking MongoDB connectivity. | Stop or misconfigure MongoDB and call `/health`. | Added a database ping and an error response when MongoDB is unavailable. |
| 2 | `compute_late_minutes()` | Late calculation does not correctly handle the required IST shift and overnight-shift rules. | Test a punch-in around the shift start, grace boundary, and overnight shift. | Reworked timestamp and shift-time calculations. |
| 3 | `compute_work_hours()` | Built-in `round()` does not guarantee the required half-up rounding. | Test durations whose decimal result is exactly at a rounding boundary. | Added explicit half-up rounding. |
| 4 | `compute_overtime()` | Does not correctly handle overnight shifts or enforce the 30-minute minimum. | Test punch-outs before, at, and after the overtime threshold on an overnight shift. | Reworked shift-end calculation and overtime threshold handling. |
| 5 | `list_employees()` | Pagination uses the wrong skip calculation, and `total` ignores the department filter. | Request page 1 and page 2; compare filtered results and totals. | Corrected pagination and filtered counting. |
| 6 | `create_employee()` | Duplicate checking before insertion is vulnerable to concurrent requests. | Send simultaneous requests with the same employee code. | Added a unique index and duplicate-key conflict handling. |
| 7 | `create_employee()` | Employee input is insufficiently validated, and `created_at` is generated as a naive datetime. | Submit invalid fields or inspect stored timestamps. | Added input validation and consistent timestamp handling. |
| 8 | `punch_in()` | The starter does not adequately protect against duplicate simultaneous punch-ins. | Send concurrent punch-in requests for the same employee and date. | Added uniqueness enforcement and conflict handling. |
| 9 | Attendance endpoints | The starter lacks robust timestamp validation and consistent handling of missing resources. | Submit malformed timestamps or request unknown employees. | Added validation and appropriate HTTP error responses. |
| 10 | Attendance calculations | The starter does not consistently enforce the required status, half-day, and overtime rules. | Test short shifts, open attendance records, and non-presence statuses. | Centralized and corrected derived-field calculations. |
| 11 | List endpoints | The starter does not provide the required filtering, sorting, pagination limits, and indexes consistently. | Test filters, invalid page sizes, and larger datasets. | Added pagination validation and indexes for common query patterns. |

## Reviewed and retained

- The IST offset is appropriate for converting between Indian calendar dates and UTC instants when applied consistently.
- A unique attendance key on `(emp_code, date)` is appropriate for preventing duplicate daily records.
- MongoDB aggregation pipelines are appropriate for analytics that combine attendance records and employee data.
- Manual corrections should append audit history; normal punch-in and punch-out operations should not create correction-history entries.

## Verification notes

The API was started successfully, MongoDB connectivity was verified, and sample employee and attendance listing requests succeeded. Monthly analytics, department summary, leaderboard, trend, and explain requests returned responses on the seeded dataset. These checks do not establish correctness for all edge cases or large datasets. Further verification of concurrency, analytics edge cases, and explain-pipeline parity remains necessary.