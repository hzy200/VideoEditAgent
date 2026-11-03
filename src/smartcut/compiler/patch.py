"""补丁应用器：EditIR + EditPatch → 新 EditIR（纯函数）。

只做结构替换，不做任何隐式时间调整；越界或清空 sequence 直接报错。
"""

from __future__ import annotations

from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.patch import EditPatch
from smartcut.errors import IRValidationError

__all__ = ["apply_patch"]


def apply_patch(ir: EditIR, patch: EditPatch) -> EditIR:
    """应用补丁并返回同 revision 内容的新对象；落库时由 Service 分配新版本号。"""
    if patch.base_revision != ir.revision:
        raise IRValidationError(
            f"补丁 base_revision={patch.base_revision} 与当前 revision={ir.revision} 不一致"
        )

    sequence = list(ir.sequence)
    for position, op in enumerate(patch.ops):
        if op.op == "insert":
            if op.index > len(sequence):
                raise IRValidationError(
                    f"ops[{position}] insert.index={op.index} 越界（长度 {len(sequence)}）"
                )
            sequence[op.index : op.index] = op.trims
            continue

        if op.index + op.count > len(sequence):
            raise IRValidationError(
                f"ops[{position}] {op.op} 区间 "
                f"[{op.index}, {op.index + op.count}) 越界（长度 {len(sequence)}）"
            )
        if op.op == "replace":
            sequence[op.index : op.index + op.count] = op.trims
        else:  # delete
            del sequence[op.index : op.index + op.count]

    if not sequence:
        raise IRValidationError("补丁不能删除全部片段")

    return EditIR(
        project_id=ir.project_id,
        revision=ir.revision,
        media_facts=ir.media_facts,
        sequence=sequence,
        effects=patch.effects if patch.effects is not None else ir.effects,
        transitions=ir.transitions,
        overlays=ir.overlays,
        removals=ir.removals,
        audio=patch.audio if patch.audio is not None else ir.audio,
        export=patch.export if patch.export is not None else ir.export,
        subtitles=ir.subtitles,
    )
