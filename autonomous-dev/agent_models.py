"""Pydantic models for structured agent output.

Each agent's final answer is requested from the Claude Agent SDK with
``output_format={"type": "json_schema", ...}`` built from these models, and
the returned ``structured_output`` is validated against the same model. The
orchestrator never parses free text to decide what happens next.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    IN_PROGRESS = "IN_PROGRESS"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class Finding(BaseModel):
    title: str
    severity: Severity
    file_path: str | None = None
    line_number: int | None = None
    explanation: str = ""
    recommendation: str = ""
    evidence: str = ""
    category: str | None = None


class TestEvidence(BaseModel):
    command: str | None = None
    test_ids: list[str] = Field(default_factory=list)
    result: str | None = None
    notes: str | None = None


class AgentOutputBase(BaseModel):
    status: Status
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    test_evidence: list[TestEvidence] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    next_action: str = ""


# ------------------------------------------------------------ product manager
class RequirementItem(BaseModel):
    title: str
    description: str
    priority: int = Field(3, ge=1, le=5, description="1 = highest priority")
    acceptance_criteria: list[str]
    related_modules: list[str] = Field(default_factory=list)
    test_cases: list[str] = Field(default_factory=list)
    source: Literal["documented", "inferred"] = "inferred"
    source_reference: str = ""
    mandatory: bool = True
    already_implemented: bool = False


class TaskItem(BaseModel):
    title: str
    description: str
    requirement_title: str
    priority: int = Field(3, ge=1, le=5)
    kind: Literal["feature", "bugfix", "security", "test", "docs", "infrastructure", "refactor"] = "feature"
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list, description="titles of tasks this one depends on")
    file_scope_hint: list[str] = Field(default_factory=list)


class ProductManagerOutput(AgentOutputBase):
    project_purpose: str = ""
    existing_features: list[str] = Field(default_factory=list)
    incomplete_functionality: list[str] = Field(default_factory=list)
    requirements: list[RequirementItem] = Field(default_factory=list)
    tasks: list[TaskItem] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, description="critical questions that block progress")
    documents_written: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ architect
class ArchitectOutput(AgentOutputBase):
    architecture_summary: str = ""
    approach: str = ""
    files_to_create: list[str] = Field(default_factory=list)
    files_to_modify: list[str] = Field(default_factory=list)
    database_changes: list[str] = Field(default_factory=list)
    api_changes: list[str] = Field(default_factory=list)
    dependencies_to_add: list[str] = Field(default_factory=list)
    technology_stack: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    security_considerations: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    requires_human_approval: bool = False
    approval_reason: str = ""
    documents_written: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ developer
class DeveloperOutput(AgentOutputBase):
    implementation_notes: str = ""
    tests_added: list[str] = Field(default_factory=list)
    docs_updated: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- code reviewer
class CriterionCheck(BaseModel):
    criterion: str
    met: bool
    evidence: str = ""


class CodeReviewOutput(AgentOutputBase):
    acceptance_criteria_met: list[CriterionCheck] = Field(default_factory=list)


# ----------------------------------------------------------------------- QA
class TestPlanItem(BaseModel):
    criterion: str
    layer: Literal["unit", "api", "database", "integration", "e2e", "qt", "gui", "acceptance"] = "unit"
    test_ids: list[str] = Field(default_factory=list)


class FailureAnalysis(BaseModel):
    test_id: str
    classification: Literal["application_bug", "test_defect", "environment"]
    explanation: str
    evidence_artifacts: list[str] = Field(default_factory=list)
    file_path: str | None = None
    line_number: int | None = None
    severity: Severity = Severity.HIGH


class QAOutput(AgentOutputBase):
    project_type_detected: str = ""
    test_files_written: list[str] = Field(default_factory=list)
    test_plan: list[TestPlanItem] = Field(default_factory=list)
    failure_analysis: list[FailureAnalysis] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)


# --------------------------------------------------------- security auditor
class FindingAssessment(BaseModel):
    finding_id: int
    verdict: Literal["VERIFIED", "LIKELY_FALSE_POSITIVE", "NEEDS_HUMAN_REVIEW"]
    evidence: str
    severity_assessment: Severity | None = None


class SecurityAuditOutput(AgentOutputBase):
    assessments: list[FindingAssessment] = Field(default_factory=list)
    manual_checks_performed: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ bug fixer
class FixItem(BaseModel):
    issue_id: int
    root_cause: str
    fix_description: str
    files_modified: list[str] = Field(default_factory=list)
    regression_test: str = ""
    reproduced: bool = False


class BugFixOutput(AgentOutputBase):
    fixes: list[FixItem] = Field(default_factory=list)
    unresolved_issue_ids: list[int] = Field(default_factory=list)


# ------------------------------------------------------------------- devops
class DevOpsOutput(AgentOutputBase):
    diagnosis: str = ""
    root_cause_category: Literal["application_code", "configuration", "environment", "dependency", "unknown"] = "unknown"
    config_changes_made: list[str] = Field(default_factory=list)
    requires_code_fix: bool = False
    health_check_notes: str = ""


# -------------------------------------------------------- product validator
class CriterionResult(BaseModel):
    criterion: str
    verdict: Literal["PASS", "FAIL", "BLOCKED"]
    evidence_test_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class ProductValidationOutput(AgentOutputBase):
    criteria_results: list[CriterionResult] = Field(default_factory=list)
    acceptance_test_files: list[str] = Field(default_factory=list)
    incomplete_requirements: list[str] = Field(default_factory=list)
    workflow_checks: list[str] = Field(default_factory=list)
    recommend_completion: bool = False


OUTPUT_MODELS: dict[str, type[AgentOutputBase]] = {
    "ProductManagerOutput": ProductManagerOutput,
    "ArchitectOutput": ArchitectOutput,
    "DeveloperOutput": DeveloperOutput,
    "CodeReviewOutput": CodeReviewOutput,
    "QAOutput": QAOutput,
    "SecurityAuditOutput": SecurityAuditOutput,
    "BugFixOutput": BugFixOutput,
    "DevOpsOutput": DevOpsOutput,
    "ProductValidationOutput": ProductValidationOutput,
}


def inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for ``model`` with all ``$ref``/``$defs`` inlined (portable for structured output)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                resolved = resolve(dict(defs[name]))
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return {**resolved, **extra}
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)
