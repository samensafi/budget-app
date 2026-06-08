# local text extraction. pdfs go through pdfplumber (tables first, then plain text).
# screenshots go through Apple's Vision framework via pyobjc with bounding box row
# reconstruction. both return None when extraction fails (scanned pdf, blurry shot)
# so callers can fall back to Claude vision.
from __future__ import annotations

import io
from typing import Optional


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    # pull text from a pdf, keeping row structure where we can. try extract_tables()
    # per page first since bank statements usually have a table, and fall back to
    # extract_text() for pages without one. returns None when the text comes back too
    # sparse, which usually means a scanned pdf the caller should send to Claude vision.
    import pdfplumber

    parts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_used_table = False
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    tables = []
                for table in tables:
                    for row in table:
                        cells = [str(c).strip() for c in row if c is not None]
                        cells = [c for c in cells if c]
                        if cells:
                            parts.append(" | ".join(cells))
                            page_used_table = True
                if not page_used_table:
                    try:
                        text = page.extract_text() or ""
                    except Exception:
                        text = ""
                    if text.strip():
                        parts.append(text)
    except Exception:
        return None

    full = "\n".join(parts).strip()
    if len(full) < 50:
        return None
    return full


def _ocr_observations(image_bytes: bytes) -> list[dict]:
    # run Apple Vision text recognition. each result is {text, bbox, confidence} where
    # bbox is (x, y_top, w, h) in normalized 0 to 1 coords with a top left origin. it
    # is already flipped from Vision's native bottom left origin so rows sort top to bottom.
    try:
        from Vision import (
            VNRecognizeTextRequest,
            VNImageRequestHandler,
            VNRequestTextRecognitionLevelAccurate,
        )
        from Quartz import CGImageSourceCreateWithData, CGImageSourceCreateImageAtIndex
        from Foundation import NSData
    except ImportError:
        return []

    nsdata = NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
    source = CGImageSourceCreateWithData(nsdata, None)
    if not source:
        return []
    cg_image = CGImageSourceCreateImageAtIndex(source, 0, None)
    if not cg_image:
        return []

    request = VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    # bank statements may have currency symbols or codes, let Vision try its best

    handler = VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
    success, _err = handler.performRequests_error_([request], None)
    if not success:
        return []

    results: list[dict] = []
    obs_list = request.results() or []
    for obs in obs_list:
        candidates = obs.topCandidates_(1)
        if not candidates or candidates.count() == 0:
            continue
        top = candidates.objectAtIndex_(0)
        bbox = obs.boundingBox()  # CGRect, normalized, bottom left origin
        y_top = 1.0 - float(bbox.origin.y) - float(bbox.size.height)
        results.append({
            "text": str(top.string()),
            "bbox": (
                float(bbox.origin.x),
                y_top,
                float(bbox.size.width),
                float(bbox.size.height),
            ),
            "confidence": float(top.confidence()),
        })
    return results


def _reconstruct_rows(observations: list[dict], y_tolerance: float = 0.014) -> list[list[str]]:
    # group the observations into rows by their y coordinate, then sort each row by x
    if not observations:
        return []
    items = sorted(observations, key=lambda o: (o["bbox"][1], o["bbox"][0]))

    rows: list[list[dict]] = []
    current = [items[0]]
    base_y = items[0]["bbox"][1]
    for item in items[1:]:
        # scale the tolerance to the line's own height so small text groups tighter
        line_h = item["bbox"][3]
        tol = max(y_tolerance, line_h * 0.4)
        if abs(item["bbox"][1] - base_y) <= tol:
            current.append(item)
        else:
            rows.append(sorted(current, key=lambda o: o["bbox"][0]))
            current = [item]
            base_y = item["bbox"][1]
    rows.append(sorted(current, key=lambda o: o["bbox"][0]))

    # joining cells with a pipe gives Claude a column hint. ocr doesn't keep the
    # spacing within a row, but column breaks read clearer when text blocks are distinct.
    return [[o["text"] for o in row] for row in rows]


def extract_image_text(
    image_bytes: bytes, min_confidence: float = 0.5
) -> tuple[Optional[str], float]:
    # returns (text, avg_confidence). text is None when ocr found nothing or the
    # average confidence came in below min_confidence, and then the caller falls back
    # to Claude vision.
    obs = _ocr_observations(image_bytes)
    if not obs:
        return None, 0.0

    avg_conf = sum(o["confidence"] for o in obs) / len(obs)

    rows = _reconstruct_rows(obs)
    text = "\n".join(" | ".join(cell for cell in row if cell) for row in rows).strip()

    if not text or len(text) < 30:
        return None, avg_conf
    if avg_conf < min_confidence:
        # return the text but flag the low confidence and let the caller decide
        return text, avg_conf
    return text, avg_conf
