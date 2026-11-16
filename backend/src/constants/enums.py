from enum import StrEnum
class Role(StrEnum):
    ADMIN='admin'
    ANALYST='analyst'
    VIEWER='viewer'
class RunStatus(StrEnum):
    QUEUED='queued'
    RUNNING='running'
    COMPLETED='completed'
    FAILED='failed'
class GenerationStatus(StrEnum):
    # Only one generation is externally visible at a time.
    # A generation queued/running/ready never changes the published result.
    QUEUED='queued'
    RUNNING='running'
    READY='ready'
    PUBLISHED='published'
    FAILED='failed'
    SUPERSEDED='superseded'
class GenerationPartStatus(StrEnum):
    PENDING='pending'
    RUNNING='running'
    BASELINE_DONE='baseline_done'
    COMPLETED='completed'
    FAILED='failed'
class CaseStatus(StrEnum):
    DRAFT='draft'
    APPROVED='approved'
INPUTS=('staff_hours','energy_cost')
OUTPUTS=('visits','programs')
DEFAULT_ALGORITHM='ccr-input-two-stage-v1'
SUPPORTED_ALGORITHMS=(DEFAULT_ALGORITHM,)
ACTIVE_GENERATION_STATUSES=(GenerationStatus.QUEUED,GenerationStatus.RUNNING)
SOLVER_STATUSES=('optimal','iteration_limit','infeasible','unbounded','numerical_error','solver_error')
