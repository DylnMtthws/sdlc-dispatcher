"""Remove unnecessary account/contact credentials from model-facing reports."""

import re

EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TOKEN = re.compile(r"\b(?:github_pat_|gh[pousr]_|sk-)[A-Za-z0-9_-]{20,}")


def model_report(report):
    return {
        key: TOKEN.sub("[credential omitted]", EMAIL.sub("[email omitted]", value))
        for key, value in report.items()
    }
