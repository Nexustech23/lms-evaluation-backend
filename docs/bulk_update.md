# Bulk update of existing students (backend only)

Update the details of students who are already enrolled, from one .xlsx or .csv file.
Institute Admin only, and only that college's own students. No AI is used anywhere in this feature.

## Steps

1. **Preview**: upload the file. Nothing is changed. You get a list of what would change, what is the same, and which rows have problems.
2. **Apply**: send the batch id with a reason (3 to 500 characters). All students are updated together, or none.
3. **History**: see every update, who did it, when, why, and whether it can still be undone.
4. **Undo**: put the old values back, all together or none. A reason is needed. Works for 7 days.

## Routes (all need the login cookie of an Institute Admin)

| Method | Path | What |
|---|---|---|
| POST | `/bulk-update/students/preview` | form field `file` |
| GET | `/bulk-update/{batch_id}` | show one preview or update |
| POST | `/bulk-update/{batch_id}/apply` | body `{"reason": "...", "skip_rejected": false}` |
| GET | `/bulk-update/history?limit=50` | newest first, 1 to 100 |
| POST | `/bulk-update/{batch_id}/undo` | body `{"reason": "..."}` |

## The file

First row = column names. One row per student.

- Required: **Enrollment No** (finds the student).
- At least one of: **Name**, **Father Name**, **DOB**, **Contact No** (common spellings are accepted).
- A **blank cell keeps the current value**.
- Other columns are ignored and listed in the preview as warnings. Enrollment No, roll no, login email and programme cannot be changed here.
- DOB: dates like 2004-05-06 or 06/05/2004; not in the future. Contact: 7 to 15 digits, may start with +.
- Limit: 2000 rows (setting `BULK_UPDATE_MAX_ROWS`). The same enrollment number twice in a file rejects both rows.

## Safety rules

- Preview changes nothing and expires after 24 hours (`BULK_UPDATE_PREVIEW_HOURS`).
- If any student was changed by someone else after the preview, nothing is applied and the rows are named. Preview again.
- If any row has a problem, apply is refused unless `skip_rejected` is true (then only the good rows are applied).
- Applying the same batch twice changes nothing the second time. Uploading an already-applied file shows a warning.
- Name and contact are also copied to the student's login account (`users`), in the same all-or-nothing step.
- Undo works for 7 days (`BULK_UPDATE_UNDO_DAYS`) and only if neither the student nor the login account was changed after the update.
- The uploaded file itself is never stored, only the planned changes and their old values.
- Limited to 10 requests a minute per user (`RATE_LIMIT_BULK_UPDATE_PER_MINUTE`).

## Also in this change

The marks import (`/import-marks-excel`) now deletes the old marks and saves the new ones as one all-or-nothing step. Its request and response are unchanged.

## Test it on the server (test students only)

Use two or three test students, never real ones. Type the admin password yourself, do not share it.
The cookie is `secure` in production, so use the https address.

```bash
BASE=https://YOUR-SERVER
# log in as an Institute Admin (use your normal login route and fields); the cookie is saved to jar.txt
curl -c jar.txt -X POST "$BASE/<your login route>" ...

# 1. preview
curl -b jar.txt -F "file=@students.xlsx" "$BASE/bulk-update/students/preview"
# 2. look at it again
curl -b jar.txt "$BASE/bulk-update/<batch_id>"
# 3. apply
curl -b jar.txt -H "Content-Type: application/json" -d '{"reason":"Test update"}' "$BASE/bulk-update/<batch_id>/apply"
# 4. check the student on the website, and that the student can still log in
# 5. history
curl -b jar.txt "$BASE/bulk-update/history"
# 6. undo
curl -b jar.txt -H "Content-Type: application/json" -d '{"reason":"Test undo"}' "$BASE/bulk-update/<batch_id>/undo"
# 7. check the student has the old values again
```

Things that should be refused (try each once):

- apply with no reason: 400
- apply twice: second time returns `already_applied: true`, nothing changes
- a file with an unknown enrollment number: that row is rejected, apply needs `skip_rejected`
- change a student on the website after the preview, then apply: 409, nothing applied
- a .pdf file, or an empty file: 400
- logging in as faculty or a student: not allowed

Before the first real use, deploy the backend (`bash redeploy-fastapi.sh`) so the new indexes are created, and keep a MongoDB backup.
