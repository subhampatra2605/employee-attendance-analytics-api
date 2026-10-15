# DECISIONS.md

1. **Indexes.** I created a unique index on employee codes to prevent duplicate employees, a unique compound index on `(emp_code, date)` to ensure one attendance record per employee per day, and indexes for common attendance queries involving dates and employee codes. These indexes support data integrity, filtering, sorting, and pagination. I would avoid adding indexes for every possible field because they increase storage and write costs.

2. **Punch-in race.** If two identical punch-in requests arrive simultaneously, both may initially find no existing record. The unique compound index allows only one insert to succeed. The other insert raises a duplicate-key error and is returned as a conflict response (`409`), rather than creating a duplicate attendance record.

3. **Ties.** The leaderboard uses MongoDB's `$rank` to assign equal ranks to employees with the same late-arrival-day count. It then sorts by rank and employee code and applies `$limit`. If the cutoff splits a tie, only the employees within the limit are returned; the remaining tied employees may be excluded.

4. **Headcount.** The summary fetches employees separately from attendance records. It includes employees whose `joined_on` date is on or before the month's last day, even if they have no attendance logs. Attendance metrics are aggregated separately for those eligible employee codes.

5. **100x data.** I would review query plans and workload metrics, then optimize aggregation pipelines and consider date-based partitioning or archival if the data volume justified it.