"""Common normalization for multipart exam creation and edits."""

import json
import math
from datetime import datetime


def finite_number(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def semester_number(value):
    number = finite_number(value, "semester")
    if number <= 0 or not number.is_integer():
        raise ValueError("semester must be a positive integer")
    return int(number)


def weightage_number(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0.0
    number = finite_number(value, "weightage")
    if not 0 <= number <= 100:
        raise ValueError("weightage must be between 0 and 100")
    return number


def exam_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("exam_date must be a valid ISO date and time")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("exam_date must be a valid ISO date and time") from None


def covered_co_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith(("[", "{", '"')) or value in ("null", "true", "false"):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError("covered_cos must be a valid JSON list") from None
        else:
            value = value.split(",")
    if not isinstance(value, list) or any(not isinstance(co, str) or not co.strip() for co in value):
        raise ValueError("covered_cos must contain non-empty CO codes")
    result = [co.strip() for co in value]
    if len(set(result)) != len(result):
        raise ValueError("covered_cos must not contain duplicate codes")
    return result


def nonblank_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def boolean_value(value, field):
    if value in (True, "true", "True", 1, "1"):
        return True
    if value in (False, "false", "False", 0, "0"):
        return False
    raise ValueError(f"{field} must be true or false")
