"""OCR worker 入口：只在子进程 import OCR 引擎。

主进程只通过 ``ocr_batch`` 的进程池调用；桌面主 exe 不 import
rapidocr/pytesseract/torch 等重依赖。
"""

from __future__ import annotations

from typing import Any

_RAPID = None
_TESSERACT = None


def _load_rapidocr():
    global _RAPID
    if _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR

        _RAPID = RapidOCR()
    return _RAPID


def _load_tesseract():
    global _TESSERACT
    if _TESSERACT is None:
        import pytesseract

        _TESSERACT = pytesseract
    return _TESSERACT


def _normalize_box(box: list[list[float]], width: int, height: int) -> tuple[float, ...]:
    xs = [float(point[0]) for point in box]
    ys = [float(point[1]) for point in box]
    left = max(0.0, min(xs) / width)
    top = max(0.0, min(ys) / height)
    right = min(1.0, max(xs) / width)
    bottom = min(1.0, max(ys) / height)
    return (
        left,
        top,
        max(0.001, right - left),
        max(0.001, bottom - top),
    )


def _ocr_rapidocr(path: str, engine: Any) -> list[dict]:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    result, _meta = engine(path)
    items: list[dict] = []
    for row in result or []:
        try:
            points, text, confidence = row[0], row[1], row[2]
        except (TypeError, IndexError):
            continue
        text = " ".join(str(text or "").split())
        if not text:
            continue
        items.append(
            {
                "text": text,
                "bbox": _normalize_box(points, width, height),
                "confidence": float(confidence or 0.0),
            }
        )
    return items


def _ocr_tesseract(path: str, engine: Any) -> list[dict]:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
        data = engine.image_to_data(
            image,
            output_type=engine.Output.DICT,
        )
    items: list[dict] = []
    for index, text in enumerate(data.get("text") or []):
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            continue
        try:
            confidence = float(data["conf"][index])
            x = float(data["left"][index]) / width
            y = float(data["top"][index]) / height
            w = float(data["width"][index]) / width
            h = float(data["height"][index]) / height
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        items.append(
            {
                "text": cleaned,
                "bbox": (
                    max(0.0, min(x, 1.0)),
                    max(0.0, min(y, 1.0)),
                    max(0.001, min(w, 1.0 - x)),
                    max(0.001, min(h, 1.0 - y)),
                ),
                "confidence": max(0.0, min(1.0, confidence / 100.0)),
            }
        )
    return items


def _select(engine_name: str) -> tuple[str, Any] | None:
    requested = (engine_name or "auto").lower()
    if requested in {"auto", "rapidocr", "rapidocr_onnxruntime"}:
        try:
            return "rapidocr", _load_rapidocr()
        except Exception:
            if requested != "auto":
                return None
            # auto 模式继续尝试 tesseract；显式记录降级路径。
            _rapidocr_error = True
    if requested in {"auto", "tesseract"}:
        try:
            return "tesseract", _load_tesseract()
        except Exception:
            return None
    return None


def ocr_batch(paths: list[str], engine_name: str = "auto") -> list[dict]:
    selected = _select(engine_name)
    if selected is None:
        message = "OCR 引擎不可用（安装 .[ocr] 或配置 tesseract）"
        return [
            {"engine": "unavailable", "items": [], "error": message}
            for _ in paths
        ]
    name, engine = selected
    results: list[dict] = []
    for path in paths:
        try:
            items = (
                _ocr_rapidocr(path, engine)
                if name == "rapidocr"
                else _ocr_tesseract(path, engine)
            )
            results.append({"engine": name, "items": items, "error": None})
        except Exception as exc:
            results.append(
                {
                    "engine": name,
                    "items": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return results


if __name__ == "__main__":  # pragma: no cover - manual worker smoke
    import json
    import sys

    payload = json.loads(sys.stdin.read() or "{}")
    print(
        json.dumps(
            ocr_batch(payload.get("paths") or [], payload.get("engine") or "auto"),
            ensure_ascii=False,
        )
    )
