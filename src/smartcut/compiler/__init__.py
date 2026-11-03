"""编译器：EditIR + MediaFacts → 受控工具调用序列（纯函数）。"""

from smartcut.compiler.lower import CompiledPlan, lower
from smartcut.compiler.patch import apply_patch
from smartcut.compiler.validate import validate_ir

__all__ = ["CompiledPlan", "apply_patch", "lower", "validate_ir"]
