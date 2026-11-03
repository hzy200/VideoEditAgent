"""编辑模板文件存储（应用级，不写项目工作区）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from smartcut.contracts.ids import is_safe_id
from smartcut.contracts.templates import EditTemplate
from smartcut.errors import IRValidationError, ResourceNotFound


class TemplateStore:
    def __init__(self, root: Path) -> None:
        self.directory = Path(root) / "templates"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, template_id: str) -> Path:
        if not is_safe_id(str(template_id)):
            raise IRValidationError(f"非法模板 id: {template_id!r}")
        return self.directory / f"{template_id}.json"

    def list(self) -> list[dict]:
        items: list[dict] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                items.append(EditTemplate.model_validate(payload).model_dump(mode="json"))
            except (OSError, ValueError):
                continue
        return items

    def get(self, template_id: str) -> EditTemplate:
        path = self._path(template_id)
        if not path.is_file():
            raise ResourceNotFound(f"模板不存在: {template_id}")
        try:
            return EditTemplate.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError) as exc:
            raise IRValidationError(f"模板文件损坏: {template_id}") from exc

    def create(self, template: EditTemplate) -> dict:
        template_id = template.id or f"tpl_{uuid4().hex[:10]}"
        path = self._path(template_id)
        if path.exists():
            raise IRValidationError(f"模板已存在: {template_id}")
        stored = template.model_copy(
            update={
                "id": template_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        path.write_text(
            json.dumps(stored.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return stored.model_dump(mode="json")

    def delete(self, template_id: str) -> dict:
        path = self._path(template_id)
        if not path.is_file():
            raise ResourceNotFound(f"模板不存在: {template_id}")
        path.unlink()
        return {"id": template_id, "deleted": True}
