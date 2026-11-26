"""Security scanning: Bandit, Semgrep and pip-audit run by the orchestrator.

These are command-line tools, not agents. The orchestrator executes them, parses
the *actual JSON reports* (a scanner's exit code alone is never treated as proof
of security), distinguishes scanner errors from findings, stores normalised
findings with stable fingerprints (so a rescan can mark findings FIXED), and
applies the configurable severity policy.

The Security Auditor agent then analyses the parsed findings; its opinion can
downgrade only LOW/MEDIUM findings by default. HIGH/CRITICAL findings can only be
waived through a human-approved entry in ``config/security_exceptions.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import CONTROLLER_DIR, REPORTS_DIR, AppConfig, resolve_path, severity_rank
from events import EventBus
from project_inspector import ProjectProfile
from safe_exec import SafeExecutor, find_executable
from state_manager import StateManager, fingerprint

log = logging.getLogger("security")

SEMGREP_SEVERITY = {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW", "CRITICAL": "CRITICAL", "HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}


@dataclass
class ScanResult:
    tool: str
    status: str  # COMPLETED | ERROR | UNAVAILABLE | SKIPPED
    findings: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int | None = None
    report_path: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    scan_id: int | None = None
    command: str = ""


@dataclass
class PolicyDecision:
    status: str  # PASS | FAIL | BLOCKED
    blocking: list[dict[str, Any]] = field(default_factory=list)
    needs_human_review: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scanner_problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"policy {self.status}", f"{len(self.blocking)} blocking"]
        if self.needs_human_review:
            parts.append(f"{len(self.needs_human_review)} need human review")
        if self.scanner_problems:
            parts.append("scanner problems: " + "; ".join(self.scanner_problems))
        return ", ".join(parts)


def _norm_path(value: str | None, workspace: Path) -> str | None:
    if not value:
        return None
    path = Path(value)
    try:
        if path.is_absolute():
            return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
    return path.as_posix().lstrip("./")


def _finding_fp(tool: str, rule: str, file_path: str | None, snippet: str) -> str:
    normalized = re.sub(r"\s+", " ", snippet or "").strip()[:300]
    return fingerprint(tool, rule, file_path or "", normalized or "")


def load_exceptions(config: AppConfig) -> dict[str, dict[str, Any]]:
    path = resolve_path(config.security.exceptions_file)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.error("security exceptions file %s is not valid JSON - ignoring it", path)
        return {}
    approved = {}
    for entry in data.get("exceptions", []):
        if entry.get("fingerprint") and entry.get("approved_by") and entry.get("reason"):
            approved[entry["fingerprint"]] = entry
        else:
            log.warning("ignoring security exception without fingerprint/approved_by/reason: %s", entry)
    return approved


class SecurityScanner:
    def __init__(self, config: AppConfig, state: StateManager, events: EventBus, executor: SafeExecutor, profile: ProjectProfile):
        self.config = config
        self.state = state
        self.events = events
        self.executor = executor
        self.profile = profile
        self.python = config.project.python_executable or sys.executable

    # -------------------------------------------------------------- helpers
    def _out_dir(self, task_id: int | None) -> Path:
        path = REPORTS_DIR / "security" / self.config.project_id / f"task-{task_id or 0}" / time.strftime("%Y%m%d-%H%M%S")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def targets(self, workspace: Path) -> list[str]:
        dirs = self.config.project.source_dirs or self.profile.source_dirs or ["."]
        return [d for d in dirs if (workspace / d).exists()] or ["."]

    def dependency_files(self, workspace: Path) -> list[Path]:
        return [workspace / r for r in self.profile.requirement_files if r.endswith(".txt") and (workspace / r).exists()]

    def dependency_hash(self, workspace: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.dependency_files(workspace)) + [workspace / "pyproject.toml"]:
            if path.exists():
                digest.update(path.read_bytes())
        return digest.hexdigest()[:24]

    # ---------------------------------------------------------------- bandit
    def run_bandit(self, workspace: Path, out_dir: Path) -> ScanResult:
        report = out_dir / "bandit-report.json"
        excludes = ",".join(str(workspace / d) for d in self.config.security.bandit.exclude_dirs + [self.config.project.test_dir])
        argv = [self.python, "-m", "bandit", "-r", *self.targets(workspace), "-f", "json", "-o", str(report), "-x", excludes, "-q"]
        result = ScanResult("bandit", "ERROR", command=" ".join(argv))
        run = self.executor.run(argv, workspace, timeout=self.config.security.bandit.timeout_seconds)
        result.exit_code = run.exit_code
        if run.error and "failed to start" in run.error:
            result.status, result.error = "UNAVAILABLE", run.error
            return result
        if not report.exists():
            result.error = f"bandit produced no report (exit {run.exit_code}): {run.summary(500)}"
            if "No module named bandit" in run.stderr:
                result.status = "UNAVAILABLE"
            return result
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            result.error = f"invalid bandit JSON: {exc}"
            return result
        result.report_path = str(report)
        if run.exit_code not in (0, 1):
            result.error = f"bandit exited with {run.exit_code}"
            return result
        for err in data.get("errors", []):
            result.notes.append(f"bandit could not scan {err.get('filename')}: {err.get('reason')}")
        for item in data.get("results", []):
            file_path = _norm_path(item.get("filename"), workspace)
            snippet = item.get("code", "")
            result.findings.append({
                "tool_name": "bandit",
                "rule_id": f"{item.get('test_id')}:{item.get('test_name')}",
                "severity": item.get("issue_severity", "MEDIUM").upper(),
                "confidence": item.get("issue_confidence", "").upper(),
                "file_path": file_path,
                "line_number": item.get("line_number"),
                "description": f"{item.get('issue_text')} ({item.get('more_info', '')})",
                "fingerprint": _finding_fp("bandit", item.get("test_id", ""), file_path, snippet),
                "report_path": str(report),
            })
        result.status = "COMPLETED"
        return result

    # --------------------------------------------------------------- semgrep
    def run_semgrep(self, workspace: Path, out_dir: Path) -> ScanResult:
        semgrep = find_executable("semgrep")
        if not semgrep:
            return ScanResult("semgrep", "UNAVAILABLE", error="semgrep executable not found (pip install semgrep)")
        local_rules = resolve_path(self.config.security.semgrep.local_rules)
        configs: list[str] = []
        if self.config.security.semgrep.use_registry:
            configs += self.config.security.semgrep.registry_configs
        if local_rules.exists():
            configs.append(str(local_rules))
        result = self._semgrep_once(semgrep, workspace, out_dir / "semgrep-report.json", configs)
        if result.status != "COMPLETED" and self.config.security.semgrep.use_registry and local_rules.exists():
            note = f"registry rulesets failed ({result.error}); re-ran with the local offline ruleset only"
            log.warning("semgrep: %s", note)
            result = self._semgrep_once(semgrep, workspace, out_dir / "semgrep-report-offline.json", [str(local_rules)])
            result.notes.insert(0, note)
            if result.status == "COMPLETED":
                result.notes.append("Semgrep ran with reduced (offline) coverage; registry rules need network access.")
        return result

    def _semgrep_once(self, semgrep: str, workspace: Path, report: Path, configs: list[str]) -> ScanResult:
        argv = [semgrep, "scan"]
        for cfg in configs:
            argv += ["--config", cfg]
        argv += ["--json", "--output", str(report), "--metrics", "off", "--disable-version-check", "--timeout", "60"]
        for excluded in self.config.security.bandit.exclude_dirs + [self.config.project.test_dir]:
            argv += ["--exclude", excluded]
        argv += self.targets(workspace)
        result = ScanResult("semgrep", "ERROR", command=" ".join(argv))
        run = self.executor.run(argv, workspace, timeout=self.config.security.semgrep.timeout_seconds, env_extra={"SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0"})
        result.exit_code = run.exit_code
        if run.timed_out:
            result.error = "semgrep timed out"
            return result
        if not report.exists():
            result.error = f"no report (exit {run.exit_code}): {run.summary(500)}"
            return result
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            result.error = f"invalid semgrep JSON: {exc}"
            return result
        result.report_path = str(report)
        fatal = [e for e in data.get("errors", []) if e.get("level", "error") == "error" and e.get("type") not in ("PartialParsing", "Syntax error", "Lexical error")]
        if run.exit_code not in (0, 1) or (fatal and not data.get("results") and not data.get("paths", {}).get("scanned")):
            result.error = "; ".join(str(e.get("message", e))[:200] for e in fatal) or f"semgrep exited with {run.exit_code}"
            return result
        for err in data.get("errors", []):
            result.notes.append(f"semgrep: {str(err.get('message', ''))[:200]}")
        for item in data.get("results", []):
            extra = item.get("extra", {})
            meta = extra.get("metadata", {}) or {}
            file_path = _norm_path(item.get("path"), workspace)
            severity = SEMGREP_SEVERITY.get(str(meta.get("impact") or extra.get("severity", "WARNING")).upper(), "MEDIUM")
            if str(extra.get("severity", "")).upper() == "ERROR" and severity == "MEDIUM":
                severity = "HIGH"
            rule = item.get("check_id", "")
            if "autodev-" in rule:  # local rules are prefixed with their (mangled) file path
                rule = rule[rule.index("autodev-"):]
            result.findings.append({
                "tool_name": "semgrep",
                "rule_id": rule,
                "severity": severity,
                "confidence": str(meta.get("confidence", "")).upper(),
                "file_path": file_path,
                "line_number": item.get("start", {}).get("line"),
                "description": extra.get("message", "")[:2000],
                "fingerprint": _finding_fp("semgrep", rule.split(".")[-1], file_path, extra.get("lines", "")),
                "report_path": str(report),
            })
        result.status = "COMPLETED"
        return result

    # ------------------------------------------------------------- pip-audit
    def run_pip_audit(self, workspace: Path, out_dir: Path) -> ScanResult:
        report = out_dir / "pip-audit-report.json"
        req_files = self.dependency_files(workspace)
        argv = [self.python, "-m", "pip_audit", "-f", "json", "-o", str(report), "--progress-spinner", "off"]
        notes = []
        if req_files:
            pinned = all(
                "==" in line or not line.strip() or line.strip().startswith(("#", "-"))
                for path in req_files
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            )
            for path in req_files:
                argv += ["-r", str(path.relative_to(workspace))]
            if pinned:
                argv.append("--no-deps")
                notes.append("all requirements pinned; audited without dependency resolution")
            else:
                notes.append("unpinned requirements; pip-audit resolved dependencies in a temporary environment")
        else:
            notes.append("no requirements file found; audited the project interpreter environment")
        result = ScanResult("pip_audit", "ERROR", command=" ".join(argv), notes=notes)
        run = self.executor.run(argv, workspace, timeout=self.config.security.pip_audit.timeout_seconds)
        result.exit_code = run.exit_code
        if "No module named pip_audit" in run.stderr:
            result.status, result.error = "UNAVAILABLE", "pip-audit is not installed in the project interpreter"
            return result
        if not report.exists():
            result.error = f"pip-audit produced no report (exit {run.exit_code}): {run.summary(600)}"
            return result
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            result.error = f"invalid pip-audit JSON: {exc}"
            return result
        result.report_path = str(report)
        dependencies = data.get("dependencies", data if isinstance(data, list) else [])
        for dep in dependencies:
            for vuln in dep.get("vulns", []):
                fixes = ", ".join(vuln.get("fix_versions", [])) or "no fixed version published"
                result.findings.append({
                    "tool_name": "pip_audit",
                    "rule_id": vuln.get("id"),
                    "severity": self.config.security.pip_audit.vulnerability_severity,
                    "confidence": "HIGH",
                    "file_path": ", ".join(str(p.relative_to(workspace)) for p in req_files) or "environment",
                    "line_number": None,
                    "description": f"{dep.get('name')} {dep.get('version')}: {vuln.get('id')} (aliases: {', '.join(vuln.get('aliases', []))}). Fixed in: {fixes}. {vuln.get('description', '')[:600]}",
                    "fingerprint": _finding_fp("pip_audit", vuln.get("id", ""), dep.get("name", ""), dep.get("version", "")),
                    "report_path": str(report),
                })
            if dep.get("skip_reason"):
                result.notes.append(f"skipped {dep.get('name')}: {dep['skip_reason']}")
        result.status = "COMPLETED"
        return result

    # ------------------------------------------------------------------ all
    def run_all(self, workspace: Path, task_id: int | None, *, dependency_scan: bool, baseline: bool = False) -> list[ScanResult]:
        toggles = self.config.testing.security
        out_dir = self._out_dir(task_id)
        results: list[ScanResult] = []
        plan = [("bandit", toggles.bandit, self.run_bandit), ("semgrep", toggles.semgrep, self.run_semgrep), ("pip_audit", toggles.pip_audit and dependency_scan, self.run_pip_audit)]
        for tool, enabled, runner in plan:
            if not enabled:
                results.append(ScanResult(tool, "SKIPPED", notes=["disabled" if tool != "pip_audit" or not toggles.pip_audit else "dependencies unchanged since last successful scan"]))
                continue
            self.events.publish("SECURITY_SCAN_STARTED", f"{tool} scan started", status="IN_PROGRESS", task_id=task_id, metadata={"tool": tool})
            result = runner(workspace, out_dir)
            result.scan_id = self.state.add_security_scan(
                self.config.project_id, task_id, tool_name=tool, status=result.status, exit_code=result.exit_code,
                findings_count=len(result.findings), report_path=result.report_path, error=(result.error or "")[:2000] or None, command=result.command[:2000],
            )
            present: set[str] = set()
            for finding in result.findings:
                present.add(finding["fingerprint"])
                finding["id"], created = self.state.upsert_security_finding(self.config.project_id, task_id, result.scan_id, finding, baseline)
                finding["new"] = created
            if result.status == "COMPLETED":
                fixed = self.state.resolve_missing_findings(self.config.project_id, tool, present)
                if fixed:
                    result.notes.append(f"{len(fixed)} previously reported finding(s) no longer present (marked FIXED)")
            self.events.publish(
                "SECURITY_SCAN_COMPLETED", f"{tool}: {result.status}, {len(result.findings)} finding(s)" + (f" - {result.error}" if result.error else ""),
                status=result.status, task_id=task_id, metadata={"tool": tool, "findings": len(result.findings), "report": result.report_path, "notes": result.notes[:5]},
            )
            results.append(result)
        if dependency_scan and any(r.tool == "pip_audit" and r.status == "COMPLETED" for r in results):
            self.state.kv_set(f"deps_hash:{self.config.project_id}", self.dependency_hash(workspace))
        summary = out_dir / "security-summary.json"
        summary.write_text(json.dumps([r.__dict__ for r in results], indent=2, default=str), encoding="utf-8")
        return results

    def needs_dependency_scan(self, workspace: Path) -> bool:
        return self.state.kv_get(f"deps_hash:{self.config.project_id}") != self.dependency_hash(workspace)

    # ---------------------------------------------------------------- policy
    def evaluate_policy(self, results: list[ScanResult], changed_files: set[str]) -> PolicyDecision:
        sec = self.config.security
        threshold = severity_rank(sec.block_severity_threshold)
        dismissible = {s.upper() for s in sec.auditor_may_dismiss_severities}
        exceptions = load_exceptions(self.config)
        decision = PolicyDecision("PASS")

        for result in results:
            if result.status in {"ERROR", "UNAVAILABLE"}:
                policy = sec.scanner_failure_policy.get(result.tool, "block")
                message = f"{result.tool} {result.status.lower()}: {result.error}"
                (decision.scanner_problems if policy == "block" else decision.warnings).append(message)
            decision.warnings.extend(n for n in result.notes if "offline" in n or "reduced" in n)

        for finding in self.state.open_security_findings(self.config.project_id):
            if finding["fingerprint"] in exceptions:
                entry = exceptions[finding["fingerprint"]]
                self.state.update_security_finding(finding["id"], status="ACCEPTED_EXCEPTION", auditor_assessment={"exception": entry})
                decision.warnings.append(f"finding #{finding['id']} waived by approved exception ({entry['approved_by']})")
                continue
            rank = severity_rank(finding["severity"])
            related = not finding["baseline"] or (finding.get("file_path") in changed_files)
            if rank < threshold:
                if finding["status"] == "VERIFIED":
                    decision.warnings.append(f"verified {finding['severity']} finding #{finding['id']} in {finding.get('file_path')}")
                continue
            if not related:
                if finding["severity"] == "CRITICAL" and sec.block_preexisting_critical:
                    decision.blocking.append(finding)
                else:
                    decision.warnings.append(f"pre-existing {finding['severity']} finding #{finding['id']} ({finding['tool_name']}) in untouched code")
                continue
            if finding["status"] == "NEEDS_REVIEW":
                decision.needs_human_review.append(finding)
                continue
            if finding["status"] == "FALSE_POSITIVE_SUSPECTED":
                if finding["severity"] in dismissible:
                    decision.warnings.append(f"#{finding['id']} dismissed by auditor as likely false positive")
                else:
                    decision.needs_human_review.append(finding)
                continue
            decision.blocking.append(finding)

        if decision.blocking:
            decision.status = "FAIL"
        elif decision.needs_human_review or decision.scanner_problems:
            decision.status = "BLOCKED"
        return decision
