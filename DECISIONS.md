# DECISIONS.md

Nothing here has been checked on a real MongoDB; the 62 integration tests that would do so have not been run (a GitHub Actions workflow to run them on MongoDB 6.0 and 7.0 is included but has not been run either).

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` (stops duplicate punch-ins, serves per-employee queries) and `(date desc, emp_code)` (list sort and month scans). `employees`: unique `emp_code`, `(department, joined_on)` (department filter, headcount) and `joined_on` (the unfiltered summary has no department to lead with). Query plans are designed, not observed.
2. **Simultaneous duplicate punch-ins.** No read-then-write. Both requests insert; the unique `(emp_code, date)` index lets one succeed and the other gets `DuplicateKeyError`, returned as 409. If that index is missing, writes answer 503.
3. **Leaderboard ties.** Employees with equal total late minutes share a rank (1, 2, 2, 4) using `$rank`. The limit applies to the rank, so a tie at the cutoff is never split and more than `limit` rows can return.
4. **Headcount.** The summary starts from `employees` (joined by month end) and looks up each one's logs, so people with no logs still count.
5. **At 100x data.** `skip` gets slower with page depth and `count_documents` rescans on every request. I would use keyset pagination on `(date, emp_code)` and cache or approximate the total.
