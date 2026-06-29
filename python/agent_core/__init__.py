"""aionet.agent_core package."""
from .agent import AgentRuntime
from .security import make_sandbox, DockerSandbox, NoneSandbox, MXCSandbox
from .loop_detector import (
    LoopDetector, AgentStep, LoopSignal, LoopSignalKind,
    make_loop_detector, detect_pattern_loop, detect_empty_loop,
    detect_semantic_loop,
)
from .task_complexity import (
    TaskComplexity, ComplexityAssessment, classify_complexity,
    get_defaults, COMPLEXITY_DEFAULTS, COMPLEXITY_DESCRIPTIONS,
)
from .prompt_builder import SystemPromptBuilder, DynamicContext

__all__ = [
    "AgentRuntime",
    "make_sandbox", "DockerSandbox", "NoneSandbox", "MXCSandbox",
    # Sprint 1
    "LoopDetector", "AgentStep", "LoopSignal", "LoopSignalKind",
    "make_loop_detector", "detect_pattern_loop", "detect_empty_loop",
    "detect_semantic_loop",
    "TaskComplexity", "ComplexityAssessment", "classify_complexity",
    "get_defaults", "COMPLEXITY_DEFAULTS", "COMPLEXITY_DESCRIPTIONS",
    "SystemPromptBuilder", "DynamicContext",
]
