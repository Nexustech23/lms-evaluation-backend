"""Keep uncertain model judgments provisional until explicit faculty resolution."""

def pending_review(answer):
    return answer.get("evaluation_status") == "pending_review" or any(
        q.get("flags", {}).get("needs_review") for q in answer.get("questionwise_marking", []))


def choice_review_required(paper_text):
    """No hidden selection policy: ambiguous optional groups require faculty review."""
    import re
    if re.search(r"(?i)\b(?:attempt|answer)\s+any\b|\beither\b", paper_text) or re.search(r"(?m)^\s*OR\s*$", paper_text):
        return True
    return any(int(attempt) < int(offered) for attempt, offered in
               re.findall(r"(?i)\b(?:attempt|answer)\s+(\d+)\s+(?:of|out of)\s+(\d+)", paper_text))


def review_draft(rules, reason):
    return {"questionwise_marking": [{"question_no": n, "max_marks": r["maxMarks"],
        "ai_awarded_marks": 0, "final_marks": 0, "parameters": [
            {"name": p["name"], "weight_percentage": p["percentage"], "ai_score": 0, "remarks": reason}
            for p in r.get("parameters", [])], "cos": [
            {"co_code": c["co_code"], "max_marks": c["marks"], "ai_marks": 0, "remarks": reason}
            for c in r.get("cos", [])], "evidence": [], "reasoning": reason,
        "feedback": "Faculty review required; this is not a final zero.", "improvement": "",
        "flags": {"needs_review": True, "unanswered": False, "incomplete": True}}
        for n, r in enumerate(rules, 1)]}
