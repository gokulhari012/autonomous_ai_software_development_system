# MASTER PROMPT: BUILD A FULLY AUTONOMOUS AI SOFTWARE DEVELOPMENT, TESTING AND DEPLOYMENT SYSTEM

## 1. Your Role

Act as a Senior AI Agent Architect, Python Developer, DevOps Engineer, Software Architect, QA Automation Engineer and Security Engineer.

I want you to design and implement a complete autonomous software development system using Claude Agent SDK (Python).

This system must independently:

1. Understand an existing software project.
2. Analyse requirements and existing code.
3. Plan development tasks.
4. Design the architecture.
5. Develop new features.
6. Review its own code through an independent reviewer.
7. Perform comprehensive automated testing.
8. Perform security testing.
9. Identify bugs and fix them.
10. Repeat testing after every fix.
11. Deploy the application to a local staging environment.
12. Validate the complete application.
13. Save progress and continue with the next task.
14. Send progress notifications through Telegram.

The system must continue working autonomously until all defined project requirements are completed or it encounters a blocker requiring human intervention.

This is an existing-application development system, not a system that only creates new projects.

**Important:** Do not simply explain how to build this system. Actually create the complete working implementation, including Python source files, agent definitions, configuration files, database, test runners, Telegram integration and documentation.

---

# 2. Execution Environment

Build the autonomous development system with the following architecture:

- Programming language: Python
- AI engine: Claude Agent SDK for Python
- Execution: Local workstation
- Operating system: Windows-compatible, with support for Linux where practical
- Persistent state: SQLite
- Version control: Git
- Application isolation: Docker where appropriate
- Testing framework: pytest
- Browser automation: Playwright
- Desktop UI automation: pytest-qt and optional PyAutoGUI
- Notifications: Telegram Bot API
- Logging: Python logging with rotating log files

The autonomous development controller must be independent of the application being developed.

Keep the agent controller in its own directory and let it operate on an existing application directory.

Suggested structure:

autonomous-dev/
    main.py
    orchestrator.py
    agent_runner.py
    state_manager.py
    task_manager.py
    security_scanner.py
    test_manager.py
    deployment_manager.py
    telegram_bot.py
    telegram_notifier.py
    config.py
    requirements.txt
    .env.example

    agents/
        product_manager.md
        architect.md
        developer.md
        code_reviewer.md
        qa_engineer.md
        security_auditor.md
        bug_fixer.md
        devops_engineer.md
        product_validator.md

    config/
        project.json
        workflow.json
        testing.json

    database/
        agent_state.db

    logs/
    reports/
        reviews/
        tests/
        security/
        releases/

    prompts/
        master_prompt.md

    scripts/

existing-application/
    CLAUDE.md
    src/
    tests/
    requirements/
    .git/

Adapt the application directory structure after inspecting the actual project. Do not assume that every application uses the same directory structure.

---

# 3. Specialized AI Agents

Create independent specialized agents. Each agent must have its own system instructions, responsibilities, permitted tools and output format.

## Agent 1: Product Manager

Responsibilities:

- Inspect existing project documentation.
- Understand the application's purpose.
- Identify incomplete functionality.
- Identify missing requirements.
- Convert requirements into development tasks.
- Create acceptance criteria.
- Prioritize tasks.
- Track completed and pending features.
- Avoid creating duplicate tasks.
- Maintain a project requirements registry.

Create a persistent requirements registry.

Each requirement must have:

- Unique ID
- Description
- Priority
- Acceptance criteria
- Related modules
- Status
- Associated test cases

Never mark a requirement complete without validation evidence.

## Agent 2: Software Architect

Responsibilities:

- Analyse the existing codebase.
- Understand the current architecture.
- Identify dependencies between modules.
- Design new features.
- Review database structures.
- Identify potential performance issues.
- Recommend maintainable implementations.
- Consider scalability and security.
- Avoid unnecessary rewrites of working code.

The Architect must respect the existing technology stack and design unless a change is justified.

## Agent 3: Developer Agent

Responsibilities:

- Implement assigned development tasks.
- Follow the existing coding conventions.
- Write maintainable and modular code.
- Implement backend and frontend functionality.
- Update database models and migrations when required.
- Handle exceptions.
- Implement validation.
- Add logging.
- Write or update unit tests.
- Update documentation.

The Developer must only work on its assigned task.

Do not allow multiple agents to modify the same files simultaneously without coordination.

## Agent 4: Independent Code Reviewer

This agent must independently review code written by the Developer.

Responsibilities:

- Review code quality.
- Identify logical errors.
- Identify duplicated code.
- Identify unnecessary complexity.
- Identify performance issues.
- Check exception handling.
- Check security practices.
- Verify database operations.
- Verify that the implementation meets acceptance criteria.

The Code Reviewer must not modify application source code.

Return:

- PASS or FAIL
- Findings
- Severity
- File and line number
- Explanation
- Recommended correction

## Agent 5: QA Testing Agent

This is a critical agent.

It must perform comprehensive functional testing, integration testing, regression testing and UI testing.

It must support BOTH:

1. Flask web applications.
2. PySide desktop applications.

It must identify the application type automatically and select the correct testing methods.

The QA Agent must not modify application source code. It can create or update test files, execute test commands and produce test reports.

Detailed testing requirements are defined in Section 6.

## Agent 6: Security Auditor Agent

Responsibilities:

- Analyse Bandit reports.
- Analyse Semgrep reports.
- Analyse dependency vulnerability reports.
- Identify security weaknesses.
- Check authentication and authorization.
- Check input validation.
- Check unsafe database queries.
- Check secrets and credential exposure.
- Check file upload security.
- Check session and token handling.
- Check insecure configurations.
- Classify vulnerabilities by severity.
- Distinguish likely false positives from verified issues.

The Security Auditor must not modify application source code.

It must provide evidence for every finding.

## Agent 7: Bug Fixer Agent

Responsibilities:

- Receive bugs from QA, Code Reviewer and Security Auditor.
- Understand the root cause.
- Reproduce the issue where possible.
- Implement the smallest appropriate fix.
- Add a regression test.
- Report the modified files.
- Explain the correction.

After every fix, the orchestrator must run the relevant tests again.

The Bug Fixer must not mark its own fix as verified.

Independent QA and security checks must verify the result.

## Agent 8: DevOps Agent

Responsibilities:

- Prepare the local staging environment.
- Manage Docker containers where appropriate.
- Start the application.
- Configure test environment variables.
- Verify database connectivity.
- Verify application startup.
- Collect startup logs.
- Check service health.
- Manage safe local staging deployment.

The DevOps Agent must not deploy to production.

## Agent 9: Product Validation Agent

This is the final independent validation agent.

Responsibilities:

- Validate the complete implemented feature against its original acceptance criteria.
- Test actual user workflows.
- Validate integration between modules.
- Validate database persistence.
- Check application startup and shutdown.
- Check for regressions.
- Verify that the application behaves as expected in staging.
- Identify incomplete requirements.

It must independently execute validation rather than trusting previous agents' claims.

Only this agent can recommend marking a requirement complete, and the orchestrator must verify its evidence before updating the task status.

---

# 4. Orchestrator and Continuous Execution

The orchestrator is the central controller.

It must manage all agents and their execution order.

Do not allow agents to independently control the entire development lifecycle.

The orchestrator owns:

- Task selection.
- Agent execution.
- Task status.
- Retry management.
- Workflow transitions.
- Persistent state.
- Git checkpoints.
- Error handling.
- Test execution.
- Deployment coordination.
- Telegram notifications.

Implement a state-machine-based workflow.

Required workflow:

PROJECT_SCAN
    ↓
REQUIREMENT_ANALYSIS
    ↓
TASK_PLANNING
    ↓
ARCHITECTURE_DESIGN
    ↓
DEVELOPMENT
    ↓
CODE_REVIEW
    ↓
QA_TESTING
    ↓
SECURITY_TESTING
    ↓
BUG_FIXING (if required)
    ↓
REPEAT QA AND SECURITY TESTING
    ↓
LOCAL_STAGING_DEPLOYMENT
    ↓
PRODUCT_VALIDATION
    ↓
GIT_CHECKPOINT
    ↓
MARK REQUIREMENT COMPLETED
    ↓
SELECT NEXT TASK
    ↓
REPEAT

Important workflow rules:

- A failed code review must return to development or bug fixing.
- Failed QA tests must go to the Bug Fixer.
- Failed security checks must go to the Bug Fixer or require security review.
- After fixing a bug, repeat relevant regression tests.
- After modifying security-sensitive code, rerun security scans.
- If staging deployment fails, analyse logs and repair the issue.
- If final validation fails, return to the relevant development stage.
- Never mark a failed task as completed.
- Never skip mandatory testing stages.

Retry policy:

- Maximum 3 repair attempts per task by default.
- After 3 unsuccessful attempts, mark the task BLOCKED.
- Send a detailed Telegram notification.
- Continue with other independent tasks if possible.
- Never silently ignore blocked tasks.

Support:

- Continuous execution.
- Maximum tasks per run.
- Maximum agent turns per task.
- Maximum execution time.
- Maximum API spending budget where measurable.
- Configurable parallel agent execution.
- Safe pause and resume.
- Recovery after application or computer restart.

Use SQLite to persist all workflow states.

The system must resume from the last confirmed state after a crash.

Before resuming, verify the Git working tree, current branch, running processes and whether the previous operation completed.

Use task locking or leases to prevent duplicate task execution.

Do not rely on in-memory state.

---

# 5. Git Management

Git integration is mandatory.

Before modifying the application:

1. Check whether Git is initialized.
2. Identify the current branch.
3. Check the working tree.
4. Identify uncommitted user changes.
5. Never overwrite or discard user changes.
6. Create a dedicated autonomous development branch or isolated Git worktree.

Use meaningful commits after successful milestones.

Suggested commit messages:

- feat: implement customer management
- fix: resolve login validation
- test: add regression coverage
- security: resolve unsafe input handling

Rules:

- Never force push.
- Never directly commit to the main branch.
- Never delete existing branches.
- Never reset or clean the working tree destructively.
- Never automatically overwrite uncommitted changes.
- Preserve developer changes.
- Create checkpoints after validated tasks.

If the project has no Git repository, explain the situation and initialise Git only after protecting existing files and obtaining a safe baseline.

---

# 6. Comprehensive Testing System

Build a modular testing system.

The QA Agent must detect the project type and automatically select appropriate testing tools.

Support:

A. Flask projects.
B. PySide6/PySide2 desktop projects.
C. Shared Python backend modules.
D. Database integration.
E. Optional browser-based UI.

Do not force Playwright on desktop applications.

## 6A. Flask Application Testing

For Flask applications, implement the following testing layers.

### Unit Testing

Use pytest.

Test:

- Individual functions.
- Business logic.
- Validation functions.
- Utility functions.
- Error handling.
- Boundary conditions.
- Exception handling.

### API Testing

Use pytest and Flask test_client or requests.

Test:

- GET requests.
- POST requests.
- PUT requests.
- PATCH requests.
- DELETE requests.
- Correct HTTP status codes.
- Request validation.
- Response structure.
- Invalid payloads.
- Missing parameters.
- Duplicate records.
- Authentication.
- Authorization.
- Expired tokens.
- Permission restrictions.
- Error responses.

### Database Testing

If PostgreSQL, MySQL or SQLite is used, test:

- Insert.
- Read.
- Update.
- Delete.
- Transactions.
- Rollbacks.
- Foreign keys.
- Constraints.
- Duplicate records.
- Data consistency.
- Concurrent operations where relevant.
- Migration correctness.

Always use an isolated test database.

Never run destructive tests against production data.

### Integration Testing

Test:

- Flask routes with services.
- Services with repositories.
- Database operations.
- Authentication with protected routes.
- Background jobs.
- External service integrations using mocks or controlled test endpoints.

### Playwright Browser Testing

Playwright is mandatory for browser-based UI testing when the Flask project has a browser interface.

Use:

- pytest-playwright.
- Chromium by default.
- Firefox and WebKit where useful and supported.

Install and configure:

pip install pytest pytest-playwright
playwright install chromium

Test actual user workflows:

- Application launches.
- Login.
- Logout.
- Navigation.
- Form submission.
- Input validation.
- Button functionality.
- Tables and pagination.
- Search and filters.
- Create, edit and delete operations.
- Dashboard data.
- Modal dialogs.
- Error messages.
- Session handling.
- Page refresh and persistence.
- Responsive layouts where applicable.

Use stable selectors such as:

- get_by_role()
- get_by_label()
- get_by_text()
- get_by_test_id()

Avoid fragile CSS selectors where possible.

Capture on failure:

- Screenshots.
- Playwright traces.
- Browser console errors.
- Relevant network failures.
- Test logs.

The QA Agent must inspect these artifacts before reporting failures.

Use deterministic, version-controlled Playwright tests for authoritative validation.

Playwright MCP or browser automation can be used for exploratory investigation, but must not replace the repeatable test suite.

## 6B. PySide Desktop Application Testing

For PySide6/PySide2 applications, implement desktop-specific testing.

Playwright must not be used for native desktop UI testing.

Use:

- pytest.
- pytest-qt.
- QtBot.
- PyAutoGUI for optional real mouse and keyboard automation.
- Screenshot capture.
- Application logs.

Install appropriate packages:

pip install pytest pytest-qt PySide6
pip install pyautogui

Detect the existing PySide version before installing or changing dependencies.

### Widget and Component Testing

Use pytest-qt to test:

- Window creation.
- Widget visibility.
- Button functionality.
- Text input.
- Password fields.
- Dropdowns.
- Combo boxes.
- Checkboxes.
- Radio buttons.
- Tables.
- Tree widgets.
- Tabs.
- Dialogs.
- Menus.
- Signals and slots.
- Keyboard shortcuts.
- Validation messages.
- Disabled and enabled states.

### UI Workflow Testing

Test complete user workflows:

- Launch the application.
- Open login screen.
- Enter credentials.
- Verify authentication.
- Open the dashboard.
- Navigate between screens.
- Create records.
- Edit records.
- Delete records.
- Search and filter.
- Validate incorrect inputs.
- Test confirmation dialogs.
- Test cancellation.
- Test application shutdown.

### Layout and Visual Testing

Test:

- Window resizing.
- Minimum window size.
- Widget visibility.
- Layout alignment.
- Screen scaling where practical.
- High-DPI behaviour.
- Missing or overlapping widgets.
- Unexpected application crashes.

Use screenshots for visual evidence.

Use PyAutoGUI when actual mouse and keyboard interaction is required, particularly for end-to-end testing of the packaged application.

### Packaged Application Testing

If the project produces an executable:

- Build the application.
- Launch the actual executable.
- Verify startup.
- Verify critical workflows.
- Verify application shutdown.
- Collect crash logs.
- Verify required assets and configuration files.

Do not assume that successful widget-level tests prove that the packaged application works.

### Headless and GUI Environment

Use Qt's offscreen platform for suitable widget-level tests where supported.

For real GUI automation, use an interactive desktop session or a dedicated virtual display.

Do not pretend that PyAutoGUI tests have run if no usable display is available.

Report unavailable GUI tests as BLOCKED rather than PASS.

## 6C. Shared Testing Rules

- Create test cases based on acceptance criteria.
- Include positive tests.
- Include negative tests.
- Include edge cases.
- Include regression tests.
- Generate test data safely.
- Clean up only test-owned data.
- Capture actual command exit codes.
- Save test reports.
- Never fabricate test results.
- Never trust a Developer Agent's claim that a test passed.
- Distinguish FAIL from BLOCKED and ERROR.
- Identify test coverage gaps.

Use pytest reports, JUnit XML and structured JSON reports where appropriate.

---

# 7. Security Testing Integration

Integrate Bandit, Semgrep and pip-audit into the autonomous development system.

These are command-line security tools, not AI agents.

The orchestrator must execute them automatically.

The Security Auditor Agent must analyse their actual output.

## 7A. Bandit

Use Bandit for Python security analysis.

Install:

pip install bandit

Run:

bandit -r src/ -f json -o reports/security/bandit-report.json

The scan must:

- Detect common Python security problems.
- Report severity and confidence.
- Identify source files and line numbers.
- Save structured reports.

Configure exclusions based on the actual project.

## 7B. Semgrep

Use Semgrep for multi-language static analysis.

Run:

semgrep scan --config auto src/ --json --output reports/security/semgrep-report.json

Use appropriate Semgrep rules for:

- Python.
- Flask.
- Web application security.
- SQL injection.
- Command injection.
- Insecure deserialization.
- Hardcoded secrets.
- Authentication weaknesses.
- Unsafe file handling.
- Common security mistakes.

Where useful, use maintained security rulesets.

Semgrep configuration may require network access or authentication depending on the selected ruleset. Handle this explicitly.

## 7C. Dependency Security

Use pip-audit for Python dependency vulnerability scanning.

Install:

pip install pip-audit

Run:

pip-audit

Where practical, export a structured report.

Check:

- Vulnerable dependencies.
- Installed versions.
- Fixed versions.
- Known security advisories.

## 7D. Security Workflow

The workflow must be:

DEVELOPMENT
    ↓
BANDIT
    ↓
SEMGREP
    ↓
PIP-AUDIT
    ↓
SECURITY AUDITOR ANALYSIS
    ↓
BUG FIXER IF REQUIRED
    ↓
REPEAT SECURITY SCANS

Important:

- Run security checks after development.
- Rerun relevant security checks after bug fixes.
- Rerun dependency scans when dependencies change.
- Do not treat scanner exit codes alone as proof of security.
- Parse the actual reports.
- Distinguish scanner errors from genuine findings.
- Distinguish false positives from verified vulnerabilities.
- Do not suppress findings just to achieve a passing result.
- Critical unresolved findings must block staging release by default.
- Define configurable severity thresholds.
- Require explicit review for security exceptions.

---

# 8. Telegram Integration

Telegram status notifications are mandatory.

Use the official Telegram Bot API.

Create a Telegram bot using BotFather.

The system must support:

- Task started.
- Task completed.
- Development progress.
- Code review failure.
- QA test failure.
- Security findings.
- Bug detected.
- Bug fixed.
- Testing passed.
- Staging deployment.
- Product validation result.
- Task blocked.
- Agent crash.
- System pause.
- Final project completion.

Avoid sending messages for every small internal agent operation.

Send meaningful milestone updates.

### Telegram Configuration

Use environment variables:

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

Never hardcode credentials.

Never commit .env files.

Use httpx or another appropriate HTTP client.

Implement:

- Message sending.
- Error handling.
- Retry for temporary failures.
- Message deduplication.
- Safe HTML formatting.
- Logging.
- Notification queue where useful.

### Example Telegram Notifications

Task started:

🚀 Autonomous Development Started

Project: Inventory Management
Task: Implement Customer Management
Agent: Developer

Status: IN PROGRESS

Testing failure:

❌ QA Testing Failed

Project: Inventory Management
Task: Customer Management

Total Tests: 25
Passed: 22
Failed: 3

Issues:
- Duplicate customer validation failed.
- Invalid email accepted.
- Customer update returned incorrect data.

Bug Fixer has been assigned.

Security issue:

⚠️ Security Findings

Tool: Semgrep
Severity: HIGH
File: src/auth.py
Issue: Potential insecure authentication pattern

Status: Sent for security review.

Task completed:

✅ Task Completed

Project: Inventory Management
Task: Customer Management

Development: PASS
Code Review: PASS
QA: PASS
Security: PASS
Staging: PASS
Validation: PASS

### Telegram Commands

Implement secure bot command handling.

Support:

/status - Overall project status
/tasks - Pending and completed tasks
/agents - Agent activity
/logs - Recent logs
/report - Latest project report
/pause - Pause new task scheduling
/resume - Resume execution
/stop - Safely stop new task execution

Only accept commands from explicitly authorized Telegram user/chat IDs.

Never expose API keys, passwords, source code secrets or sensitive logs through Telegram.

Use long polling for receiving commands on a local workstation unless a secure webhook is specifically required.

Pause should finish the current safe operation before pausing.

Stop should prevent new tasks from starting and shut down gracefully.

---

# 9. Database and Persistent State

Use SQLite for the autonomous controller's persistent state.

Create appropriate tables.

Suggested tables:

tasks
- id
- title
- description
- priority
- status
- retry_count
- requirement_id
- assigned_agent
- created_at
- updated_at
- completed_at

agent_runs
- id
- task_id
- agent_name
- status
- input_summary
- output_summary
- started_at
- finished_at
- error_message

issues
- id
- task_id
- title
- severity
- source_agent
- file_path
- line_number
- description
- status
- fix_attempts

checkpoints
- id
- task_id
- git_commit
- description
- created_at

test_runs
- id
- task_id
- test_type
- tool_name
- status
- total
- passed
- failed
- blocked
- report_path
- created_at

security_findings
- id
- task_id
- tool_name
- severity
- file_path
- line_number
- description
- status
- report_path
- created_at

requirements
- id
- description
- priority
- acceptance_criteria
- status
- validation_evidence

notifications
- id
- event_type
- message
- status
- created_at
- sent_at

You may modify the schema when necessary.

Use database migrations or a versioned schema strategy.

Ensure that task transitions are transactional and persistent.

---

# 10. Agent Communication and Structured Output

Do not make agents communicate using arbitrary text parsing.

Use structured JSON responses.

Validate responses using Pydantic models.

Each agent must return:

- Status.
- Summary.
- Findings.
- Modified files.
- Test evidence where applicable.
- Recommendations.
- Errors.
- Next action.

Use controlled status values such as:

PASS
FAIL
BLOCKED
ERROR
IN_PROGRESS

The orchestrator must independently validate important results.

For example:

- A test is PASS only if the actual test execution succeeded.
- A security scan is PASS only if it ran successfully and the configured severity policy is satisfied.
- A deployment is PASS only if health checks succeed.
- A requirement is COMPLETED only if independent acceptance validation succeeds.

Do not rely solely on AI-generated claims.

---

# 11. Local Staging and Deployment

Implement a local staging deployment system.

For Flask:

- Start the Flask application in a separate staging environment.
- Use a separate test/staging database.
- Verify health endpoints.
- Check application logs.
- Execute API tests against staging.
- Execute Playwright tests against staging.

For PySide:

- Prepare the desktop test environment.
- Launch the application.
- Execute pytest-qt tests.
- Execute GUI automation where supported.
- Test the packaged executable where applicable.
- Collect application logs and screenshots.

Use Docker Compose for Flask and other services where appropriate.

Do not force Docker on a native PySide application.

Production deployment must remain disabled.

Never automatically:

- Deploy to a production server.
- Modify a production database.
- Delete production data.
- Change production credentials.
- Publish releases.
- Push directly to main.
- Execute destructive migrations on real customer data.

These actions require explicit human authorization.

---

# 12. Error Handling and Recovery

The system must handle:

- Claude API failures.
- Network failures.
- Agent timeouts.
- Invalid JSON responses.
- Missing dependencies.
- Test runner crashes.
- Application startup failures.
- Database connection failures.
- Telegram API failures.
- Computer restart.
- Interrupted development sessions.

Use:

- Timeouts.
- Bounded retries.
- Exponential backoff for transient errors.
- Structured logs.
- Persistent state.
- Task locks.
- Graceful shutdown.
- Recovery checks.

Do not repeat the same failed action indefinitely.

Do not mark a task complete after an exception.

If an operation has an uncertain outcome, inspect the actual environment before retrying it.

---

# 13. Security and Execution Permissions

This system will run autonomously, so implement strict execution boundaries.

The system may autonomously:

- Read application source code.
- Modify application files within the configured project directory.
- Create tests.
- Execute approved test commands.
- Run approved security scanners.
- Start local staging services.
- Create Git branches and commits.
- Generate reports.
- Send Telegram notifications.

The system must not have unrestricted access to the workstation.

Implement:

- Dedicated project workspace.
- Dedicated OS user where practical.
- Command allowlisting.
- Filesystem restrictions.
- Tool permissions per agent.
- Docker isolation where practical.
- Separate staging credentials.
- Secret management through environment variables.
- API usage limits.
- Runtime limits.
- Audit logs.

Do not rely on Claude permission settings alone as a security boundary.

Do not expose arbitrary shell access to every agent.

Use narrowly scoped tools and explicit command validation.

The Code Reviewer, QA Agent and Security Auditor should have read-only access to application source files.

The QA Agent may write to the test directory and reports directory.

The Bug Fixer may modify application source files only within its assigned scope.

Never allow an agent to access unrelated personal files or directories.

---

# 14. Configuration Files

Create a configurable project.json.

Example:

{
  "project_name": "Existing Application",
  "project_path": "../existing-application",
  "application_types": [
    "flask",
    "pyside"
  ],
  "default_branch": "main",
  "development_branch": "autonomous-development",
  "staging_environment": "local",
  "deployment_method": "docker-compose",
  "max_retries_per_task": 3,
  "max_parallel_agents": 2,
  "max_tasks_per_run": 100,
  "production_deployment": false,
  "require_git_checkpoint": true,
  "require_independent_review": true,
  "require_acceptance_validation": true
}

Create testing.json:

{
  "python": {
    "enabled": true,
    "framework": "pytest"
  },
  "flask": {
    "api_testing": true,
    "database_testing": true,
    "integration_testing": true,
    "playwright": true
  },
  "pyside": {
    "enabled": true,
    "pytest_qt": true,
    "pyautogui": true,
    "packaged_application_testing": true
  },
  "security": {
    "bandit": true,
    "semgrep": true,
    "pip_audit": true
  },
  "reports": {
    "json": true,
    "junit_xml": true,
    "screenshots": true
  }
}

Detect actual project technologies and adapt the configuration.

Do not enable irrelevant testing tools just because they appear in the configuration.

For example:

- A Flask project with a browser interface should use Playwright.
- A Flask API-only project should use API tests; browser testing can be skipped.
- A PySide project should use pytest-qt.
- PyAutoGUI should be used when actual desktop interaction is needed and a GUI session is available.
- Bandit and Semgrep should scan applicable source code.
- pip-audit should inspect Python dependencies.

---

# 15. Project Completion Criteria

The autonomous system must not decide that a project is complete merely because the code runs.

A project is complete only when:

1. All mandatory requirements have been implemented.
2. All mandatory acceptance criteria have been validated.
3. Code review has passed.
4. Required unit tests have passed.
5. Required integration tests have passed.
6. Required API tests have passed.
7. Required UI tests have passed or are explicitly classified as not applicable.
8. Required security scans have completed.
9. No unresolved critical security issues remain.
10. Local staging validation has passed.
11. No known critical regressions remain.
12. Documentation is updated.
13. Git checkpoints exist.
14. A final project report is generated.

Any incomplete or blocked requirement must be clearly listed.

Never hide incomplete work to produce a successful completion report.

---

# 16. Final Project Report and Telegram Summary

When all tasks are completed, generate a final report containing:

- Project overview.
- Requirements completed.
- Features implemented.
- Files modified.
- Git commits.
- Architecture changes.
- Test results.
- Flask API testing results.
- Playwright testing results.
- PySide UI testing results.
- Bandit findings.
- Semgrep findings.
- pip-audit findings.
- Bugs identified and fixed.
- Remaining issues.
- Blocked requirements.
- Staging deployment results.
- Final validation results.

Save reports under:

reports/releases/

Send a concise final summary to Telegram.

---

# 17. Implementation Instructions: Build in Phases

Do not try to implement the entire system in one giant unverified code-generation step.

Build it incrementally.

### Phase 1: Foundation

- Inspect the workstation environment.
- Verify Python version.
- Verify Git.
- Verify Claude Agent SDK availability.
- Create project structure.
- Create configuration system.
- Create SQLite state manager.
- Create structured logging.
- Create safe command execution module.

### Phase 2: Agent Framework

- Implement AgentRunner.
- Create all nine agent definitions.
- Configure agent permissions.
- Implement structured Pydantic output validation.
- Test agent communication.

### Phase 3: Orchestrator

- Implement workflow state machine.
- Implement task manager.
- Implement retry logic.
- Implement Git checkpoint management.
- Implement persistent recovery.
- Test interruption and resumption.

### Phase 4: Testing Framework

- Integrate pytest.
- Integrate Flask API testing.
- Integrate database testing.
- Integrate Playwright.
- Integrate pytest-qt.
- Integrate optional PyAutoGUI.
- Integrate screenshot and log collection.
- Validate test report generation.

### Phase 5: Security Framework

- Integrate Bandit.
- Integrate Semgrep.
- Integrate pip-audit.
- Implement report parsing.
- Implement Security Auditor workflow.
- Implement security severity policies.

### Phase 6: Telegram

- Implement bot configuration.
- Implement notification service.
- Implement authorized command handling.
- Implement status reporting.
- Test notifications and commands.

### Phase 7: Deployment and Validation

- Implement local Flask staging deployment.
- Implement PySide launch and validation.
- Implement application health checks.
- Implement independent Product Validator.
- Implement final reporting.

### Phase 8: Full Integration Test

Create a small sample Flask application and a small sample PySide application for testing the autonomous development system itself.

Verify:

- A task can be planned.
- A developer can implement a change.
- The reviewer can identify a deliberately introduced issue.
- QA can generate and execute tests.
- Playwright can test a Flask browser workflow.
- pytest-qt can test a PySide window.
- Bandit and Semgrep can identify deliberately introduced test vulnerabilities.
- The Bug Fixer can correct a controlled issue.
- Tests are rerun after the correction.
- Telegram notifications work.
- The system can restart and resume.
- A task can be marked completed only after independent validation.

Use harmless, isolated sample vulnerabilities and test data. Do not introduce vulnerabilities into a real application.

---

# 18. Critical Development Instructions

Follow these rules throughout implementation:

1. First inspect the existing environment and available SDK documentation.
2. Use the current supported Claude Agent SDK APIs. Do not invent SDK methods or parameters.
3. Check package versions and installation requirements.
4. Build working modules, not placeholder implementations.
5. Do not leave important functions as TODOs.
6. Do not use hardcoded credentials.
7. Do not use fake testing results.
8. Do not allow agents to mark their own work as independently validated.
9. Do not allow unlimited retry loops.
10. Do not allow concurrent agents to corrupt shared files.
11. Do not overwrite existing user work.
12. Do not deploy to production.
13. Keep the architecture modular and extensible.
14. Provide clear setup and execution instructions.
15. Provide useful error messages.
16. Run actual tests on the autonomous controller itself.
17. Verify generated files and commands before declaring implementation complete.

If a dependency, API, feature or environment capability is unavailable, clearly report the limitation and implement a safe alternative where possible.

Do not assume that a Claude subscription automatically provides API access for the Claude Agent SDK. Verify the required authentication and billing configuration.

---

# 19. Start Now

Begin by inspecting the current working directory and the available environment.

Then:

1. Identify the existing application and its technology stack.
2. Determine whether it is Flask, PySide or a combination.
3. Identify its current tests and Git status.
4. Propose the implementation structure.
5. Create the autonomous development controller in its own directory.
6. Implement the system in the phases described above.
7. Run tests after each phase.
8. Provide a final setup guide and launch instructions.

Do not stop after creating agent prompt files.

The goal is a working, persistent, autonomous AI software development system that can develop, review, test, secure, fix, deploy to local staging and validate Flask and PySide applications repeatedly until all defined requirements are completed.


# ADDITIONAL REQUIREMENT: SUPPORT NEW AND EXISTING PROJECT DEVELOPMENT

The autonomous development system must support two modes.

## MODE A: NEW PROJECT DEVELOPMENT

When a user requests a new software project, the system must create the application from scratch.

### Step 1: Project Discovery

Understand:
- Project purpose.
- Business requirements.
- Target users.
- Required features.
- User workflows.
- Technology preferences.
- Database requirements.
- External integrations.
- UI requirements.
- Deployment requirements.

If critical information is missing, ask the user before making major architectural decisions.

Do not invent important business requirements.

### Step 2: Project Specification

Create:
- Product requirements document.
- Functional requirements.
- Non-functional requirements.
- User workflows.
- Feature list.
- Acceptance criteria.
- Database requirements.
- Security requirements.
- Testing strategy.

Save these documents in the project documentation directory.

### Step 3: Architecture Design

The Architect must:
- Select an appropriate architecture.
- Recommend a suitable technology stack.
- Design database tables and relationships.
- Define backend modules.
- Define frontend or desktop UI modules.
- Define API contracts.
- Define integrations.
- Define security boundaries.
- Define deployment architecture.

Respect the user's technology preferences.

If no technology is specified, recommend one with reasons and seek confirmation before committing to major technology choices.

### Step 4: Project Initialization

Create:
- Project directory.
- Git repository.
- Source code structure.
- Configuration files.
- Environment variable template.
- Database models.
- Initial migrations.
- Logging system.
- Error handling.
- Dependency management.
- README documentation.
- Initial automated tests.

Never put real credentials in source code.

### Step 5: Feature Development

Break the project into independent development tasks.

Prioritize:
1. Core infrastructure.
2. Database.
3. Backend.
4. API.
5. Frontend or desktop UI.
6. Authentication.
7. Business logic.
8. Integrations.
9. Reporting.
10. Additional features.

Adapt this order to the actual application.

Implement one feature at a time and validate each feature before proceeding.

### Step 6: Complete Testing

Use the existing testing architecture:

For Flask:
- pytest.
- API testing.
- Database testing.
- Integration testing.
- Playwright browser testing where applicable.

For PySide:
- pytest.
- pytest-qt.
- QtBot.
- PyAutoGUI where required.
- Actual packaged application testing.

For security:
- Bandit.
- Semgrep.
- pip-audit.

### Step 7: Product Completion

Continue development until all mandatory requirements are implemented and independently validated.

Generate documentation and a final project report.

## MODE B: EXISTING PROJECT DEVELOPMENT

When the user provides an existing application:

- Inspect the project before making changes.
- Analyse its current architecture.
- Understand existing database structures.
- Identify existing features.
- Identify incomplete functionality.
- Inspect current tests.
- Inspect Git status.
- Preserve existing user changes.
- Do not unnecessarily rewrite working modules.
- Implement only approved requirements.
- Run regression tests after changes.

## MODE SELECTION

Add the following configuration:

{
  "project_mode": "auto",
  "supported_modes": [
    "new",
    "existing"
  ],
  "require_project_discovery": true,
  "require_architecture_design": true,
  "require_acceptance_criteria": true
}

Mode behaviour:

- new: Create the project from scratch.
- existing: Inspect and modify an existing application.
- auto: Detect the project condition and ask the user if it is ambiguous.

## IMPORTANT

Both modes must use the same:

- Agent framework.
- Orchestrator.
- SQLite state management.
- Git management.
- QA testing system.
- Playwright integration.
- PySide testing.
- Bandit and Semgrep security scanning.
- Bug Fixer.
- DevOps Agent.
- Telegram notifications.
- Product Validator.
- Recovery and retry mechanism.

Do not create separate autonomous controllers for new and existing projects.



# ADDITIONAL REQUIREMENT: AUTONOMOUS DEVELOPMENT MONITORING DASHBOARD

Build a complete web-based monitoring and control dashboard for the autonomous development system.

The dashboard must be implemented as a separate application from the autonomous development controller.

## Technology

Frontend: React
Backend: FastAPI
Real-time communication: WebSockets
Database: Existing SQLite state database
Charts: Recharts
Authentication: Local dashboard authentication

Default address: http://127.0.0.1:8000

## Dashboard Modules

1. Overview Dashboard
   - Current project
   - Project mode: New or Existing
   - Overall progress
   - Active agents
   - Completed tasks
   - Failed tasks
   - Blocked tasks
   - Current workflow stage
   - Recent activity

2. Agent Monitor
   - Display all agents.
   - Show agent status.
   - Show assigned task.
   - Show current activity.
   - Show execution duration.
   - Show last activity time.
   - Show errors and retry counts.
   - Display agent input/output summaries.
   - Allow inspection of agent execution history.

3. Live Console
   - Display real-time agent events.
   - Display tool execution events.
   - Display command execution results.
   - Display errors and exceptions.
   - Support log filtering.
   - Support searching.
   - Allow viewing complete execution logs.

4. Task Manager
   - List all tasks.
   - Show task dependencies.
   - Show priority.
   - Show assigned agent.
   - Show status.
   - Show retry count.
   - Show acceptance criteria.
   - Allow safe task cancellation and retry.
   - Allow inspection of task history.

5. Testing Center
   - Display pytest results.
   - Display Flask API test results.
   - Display Playwright test results.
   - Display pytest-qt results.
   - Display PyAutoGUI test results.
   - Display database test results.
   - Show screenshots.
   - Show Playwright traces.
   - Show test reports.
   - Display test coverage when available.

6. Security Center
   - Display Bandit findings.
   - Display Semgrep findings.
   - Display pip-audit findings.
   - Show severity.
   - Show file and line number.
   - Show vulnerability details.
   - Show fix status.
   - Show security scan history.

7. Git Management
   - Current branch.
   - Commit history.
   - Modified files.
   - Latest checkpoint.
   - Task-to-commit mapping.
   - Show Git status.
   - Never allow destructive Git actions without explicit authorization.

8. Deployment Monitor
   - Staging status.
   - Application health.
   - Container status where applicable.
   - Startup logs.
   - Deployment history.
   - Validation status.

9. Usage and Cost Monitor
   - Claude API usage where available.
   - Input/output token counts where available.
   - Estimated API cost.
   - Agent execution duration.
   - Retry count.
   - Total project execution time.

## Control Panel

Provide:

- Start execution.
- Pause execution.
- Resume execution.
- Stop execution safely.
- Start a new project.
- Open an existing project.
- Select project.
- Retry failed task.
- Cancel pending task.
- Generate final report.

Restrict sensitive and destructive operations.

## Real-Time Event Architecture

The orchestrator must publish structured events whenever important activities occur.

Examples:

AGENT_STARTED
AGENT_PROGRESS
AGENT_TOOL_STARTED
AGENT_TOOL_COMPLETED
AGENT_COMPLETED
AGENT_FAILED
TASK_CREATED
TASK_STARTED
TASK_COMPLETED
TASK_BLOCKED
TEST_STARTED
TEST_COMPLETED
SECURITY_FINDING
BUG_CREATED
BUG_FIXED
DEPLOYMENT_STARTED
DEPLOYMENT_COMPLETED
VALIDATION_COMPLETED
SYSTEM_PAUSED
SYSTEM_RESUMED
SYSTEM_STOPPED

Each event must contain:

- Event ID.
- Timestamp.
- Project ID.
- Task ID where applicable.
- Agent name where applicable.
- Event type.
- Status.
- Relevant summary.
- Structured metadata.

Store important events persistently in SQLite.

Use WebSockets to push updates to the frontend.

Implement reconnection and recovery so the dashboard can reconnect without losing historical events.

Do not rely on WebSocket messages as the only source of truth. The database must remain authoritative.

## Agent Monitoring

Capture actual agent lifecycle events, tool usage and results from the Claude Agent SDK wherever supported.

Record orchestrator events for task transitions, test execution and deployments.

Clearly distinguish actual agent activity from inferred progress.

Do not invent progress percentages or show an agent as active when it is not executing.

## Console

Create a terminal-style interface that shows real agent activity.

Display:
- Agent name.
- Task.
- Timestamp.
- Activity.
- Tool being executed.
- Execution result.
- Errors.

Provide filters for each agent and task.

Do not expose secrets, environment variables, API keys or sensitive file contents.

## Safety

- Bind to localhost by default.
- Require authentication for dashboard controls.
- Use authorization checks for sensitive operations.
- Validate all WebSocket messages.
- Prevent unauthorized command execution.
- Do not provide unrestricted shell access through the dashboard.
- Do not expose the dashboard publicly without additional security configuration.
- Keep production deployment disabled.

## Implementation Requirements

Implement the dashboard in phases.

1. Backend event API.
2. Persistent event storage.
3. WebSocket event streaming.
4. Agent monitor.
5. Task manager.
6. Live console.
7. Testing and security dashboards.
8. Git and deployment monitor.
9. Control panel.
10. Authentication and security testing.

Test the dashboard using actual orchestrator events.

Do not create a static dashboard with fake agent data.

The dashboard must monitor the same running agents controlled by the orchestrator.

The Telegram notification system must continue working independently alongside the dashboard.