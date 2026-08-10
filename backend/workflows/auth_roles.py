from __future__ import annotations


ANALYSIS_OPERATOR_GROUP = "analysis-operators"

ALL_SECTIONS = ("edit", "tools", "packages", "artifacts", "runs", "wdl", "help")
ANALYSIS_OPERATOR_SECTIONS = ("runs",)


def is_admin(user) -> bool:
    return bool(user.is_superuser or user.is_staff)


def is_analysis_operator(user) -> bool:
    return bool(
        user.is_authenticated
        and user.groups.filter(name=ANALYSIS_OPERATOR_GROUP).exists()
    )


def user_role(user) -> str:
    if is_admin(user):
        return "admin"
    if is_analysis_operator(user):
        return "analysis_operator"
    return "restricted"


def allowed_sections(user) -> tuple[str, ...]:
    if is_admin(user):
        return ALL_SECTIONS
    if is_analysis_operator(user):
        return ANALYSIS_OPERATOR_SECTIONS
    return ()
