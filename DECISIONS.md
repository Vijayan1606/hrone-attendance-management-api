# Decisions

### 1. Indexes
I created a unique index on `employees.emp_code` to prevent duplicate employee codes, and a unique compound index on `attendance(emp_code, date)` to prevent duplicate daily attendance. For employee filtering, I added indexes on `(department, emp_code)`, `(department, joined_on)`, and `joined_on`. For attendance queries, I added indexes on `(date DESC, emp_code ASC)`, `(status, date DESC)`, and `(emp_code, punch_in DESC)`. I considered indexing `late_minutes` for the leaderboard but rejected it because the leaderboard calculates total late minutes through aggregation, so a simple index would not directly optimize its final ranking.

### 2. Punch-in race
If two identical punch-in requests arrive simultaneously, both may initially find no attendance record. Both attempt to insert a record, but the unique `(emp_code, date)` index allows only one insert. The other request receives a duplicate-key error, which the API converts into an HTTP 409 Conflict response.

### 3. Ties
The leaderboard uses competition ranking. Employees tied on total late minutes receive the same rank. Since the limit is applied after ranking, tied employees at the cutoff are all returned, even if the result contains more rows than the requested limit.

### 4. Headcount
The department summary starts with employees rather than attendance records. It then aggregates attendance data, allowing employees with no logs to remain included in headcount.

### 5. Scaling
For 100x more data, I would benchmark the aggregation pipelines with a large dataset, inspect execution plans, and optimize expensive stages and indexes based on measured query performance.
