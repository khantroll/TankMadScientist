"""Detect low-quality model output (meta-plans, empty templates)."""
import re

META_STEP = re.compile(
    r"\d+\.\s+"
    r"(Review|Check|Validate|Assess|Ensure|Identify|Test|Summarize|Provide|Analyze)\b",
    re.I,
)
FILE_REF = re.compile(
    r"\b[\w./\\-]+\.(ps1|psm1|py|js|ts|md|sh|yaml|yml|json|txt)\b",
    re.I,
)
BRACKET_PLACEHOLDER = re.compile(r"\[[A-Za-z][^\]]{2,}\]")


def looks_like_meta_plan(text: str) -> bool:
    """True when the model returned a procedure checklist, not a deliverable."""
    if not text or not text.strip():
        return True

    stripped = text.strip()
    if len(stripped) < 50:
        return True

    placeholders = BRACKET_PLACEHOLDER.findall(text)
    if len(placeholders) >= 3:
        return True

    steps = META_STEP.findall(text)
    file_refs = FILE_REF.findall(text)
    if len(steps) >= 3 and len(file_refs) < 2:
        return True

    if len(steps) >= 2 and len(stripped) < 500 and len(file_refs) == 0:
        return True

    return False


RETRY_NUDGE = """
Your previous answer was REJECTED: it was a procedure checklist or empty template, not a deliverable.

Return your COMPLETE answer now as markdown with:
## Executive summary
## Findings (each must cite a specific filename and actual code behavior)
## Recommendations (prioritized, actionable)

Do NOT list steps you would take. Report what you found in the files provided.
"""
