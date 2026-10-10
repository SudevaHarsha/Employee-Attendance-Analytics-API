# DECISIONS.md

**Indexes.** `employees.emp_code` (unique) enforces unique codes and serves lookup by code; `{department, emp_code}` serves the department-filtered employee list; `{department, joined_on}` serves department headcount and the trend (R9). On `attendance_logs`: `{emp_code, date}` (unique) enforces one record per employee-day and powers the punch-in race; `{date, emp_code}` (desc) serves the list sort and month ranges; `{status, date, emp_code}` serves the status-filtered list. I rejected a standalone `{status}` index: low selectivity, and no main plan uses it alone.

**Punch-in race.** The unique `{emp_code, date}` index plus a single `insert_one`: two identical requests both attempt the insert, the database accepts exactly one and raises `DuplicateKeyError` for the other, which I return as 409. There is no check-then-insert gap.

**Ties.** The leaderboard sorts by total late minutes descending, then `emp_code` ascending, and assigns standard competition ranks (1, 2, 2, 4). `limit` keeps every row with `rank <= limit`, so a tie at the cutoff returns more rows than `limit`.

**Headcount.** The summary starts from `employees` with `joined_on <= month end`, groups by department, then joins logs for the metrics, so zero-log employees still count.

**At 100x.** I would precompute daily and monthly rollups into summary collections, read instead of scanning logs.
