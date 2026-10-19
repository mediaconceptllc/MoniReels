"""pip-audit's JSON as the job summary, plus one annotation per finding.

    python3 pip_audit_report.py <audit.json> <image name>

Exits 1 when a finding is left. pip-audit has already dropped every ID the
workflow passed with --ignore-vuln, so what remains is what blocks. The
annotations matter: they come with the check run, and the job log may not
be readable where the check is.
"""
from __future__ import annotations

import json
import os
import sys


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: str, image: str) -> int:
    lines = [f"### pip-audit: {image}", ""]
    try:
        with open(path, encoding="utf-8") as handle:
            dependencies = json.load(handle).get("dependencies", [])
    except (OSError, ValueError) as exc:
        lines.append(f"pip-audit did not produce a report ({exc}).")
        print(f"::error title=pip-audit::{escape(f'{image}: no report: {exc}')}")
        dependencies, status = [], 2
    else:
        status = 0
    findings = [(dep, vuln) for dep in dependencies for vuln in dep.get("vulns", [])]
    if findings:
        lines += ["| package | version | advisory | fixed in |", "|---|---|---|---|"]
        for dep, vuln in findings:
            names = ", ".join([vuln["id"], *vuln.get("aliases", [])])
            fixed = ", ".join(vuln.get("fix_versions", [])) or "no fix yet"
            lines.append(f"| {dep['name']} | {dep['version']} | {names} | {fixed} |")
            print(
                "::error title=pip-audit::"
                + escape(f"{image}: {dep['name']} {dep['version']}: {names} (fixed in {fixed})")
            )
        status = 1
    elif status == 0:
        lines.append(f"No known advisory in the {len(dependencies)} installed packages.")
    skipped = [dep for dep in dependencies if dep.get("skip_reason")]
    if skipped:
        lines += ["", "Not checked:", *(f"- {dep['name']}: {dep['skip_reason']}" for dep in skipped)]
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
