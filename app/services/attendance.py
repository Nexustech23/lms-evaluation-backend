"""How a student's attendance status on an exam affects results and attainment.

Statuses are written by manual marks entry (services/manual_marks.py). An answer
with no `attendance_status` (every row saved before this feature) is "present",
so existing exams behave exactly as before.

- absent: did not sit the exam. Excluded from CO attainment; excluded from the
  weighted course result, unless the exam is mandatory (then the result stays
  incomplete until the student clears it).
- malpractice: marks are 0. A real 0 in the weighted course result, but excluded
  from CO attainment so cheating does not distort outcome data.
"""

ATTENDANCE_STATUSES = ("present", "absent", "malpractice")


def attendance_status(answer):
    status = (answer or {}).get("attendance_status")
    return status if status in ("absent", "malpractice") else "present"


def counts_for_attainment(answer):
    """Only students who sat the exam honestly feed CO attainment."""
    return attendance_status(answer) == "present"


def is_not_cleared(exam, status, percentage):
    """True when a mandatory exam has not been cleared by this student.

    percentage is None while the student's marks are still pending; that is
    unknown, not a failure, so it is not flagged."""
    if not (exam or {}).get("is_mandatory"):
        return False
    if status in ("absent", "malpractice"):
        return True
    minimum = (exam or {}).get("min_clear_percent")
    return minimum is not None and percentage is not None and percentage < minimum
