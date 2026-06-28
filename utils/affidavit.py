"""
Name-Mismatch Affidavit metadata generator.

When the document audit flags a cross-document name mismatch, the citizen can
resolve it by submitting a sworn Name Mismatch Affidavit (a common Indian legal
remedy on stamp paper, attested by a Notary / Gazetted Officer). This module
turns the raw anomaly strings produced by `document_audit_node` into a clean,
structured, bilingual (English + Kannada) legal template that the frontend
`AffidavitViewer` component renders print-ready.

Anomaly string format produced upstream (agent_graph.document_audit_node):
    "Name mismatch: '<src_name>' on <src_doc> vs '<tgt_name>' on <tgt_doc> "
    "(Jaro-Winkler=<score>, threshold=<th>)"
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Strips ASCII control characters and common injection characters from OCR-derived names
# before they are embedded into a legal document template.
_UNSAFE_CHARS_RE: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f-\x9f<>\"']")


def _sanitize_name(name: str) -> str:
    return _UNSAFE_CHARS_RE.sub("", name).strip()[:256]

# Captures every "'<name>' on <doc_type>" pair inside an anomaly line.
_NAME_ON_DOC_RE: re.Pattern[str] = re.compile(r"'([^']+)'\s+on\s+([a-z_]+)")

# Preference order when picking the "other" (non-Aadhaar) name for the affidavit
_OTHER_DOC_PRIORITY: list[str] = ["ration_card", "bank_passbook", "land_record"]

# Human-readable document labels (English + Kannada).
_DOC_LABELS_EN: dict[str, str] = {
    "aadhaar": "Aadhaar Card",
    "ration_card": "Ration Card",
    "bank_passbook": "Bank Passbook",
    "land_record": "Land Record (RTC)",
}
_DOC_LABELS_KN: dict[str, str] = {
    "aadhaar": "ಆಧಾರ್ ಕಾರ್ಡ್",
    "ration_card": "ಪಡಿತರ ಚೀಟಿ",
    "bank_passbook": "ಬ್ಯಾಂಕ್ ಪಾಸ್‌ಬುಕ್",
    "land_record": "ಭೂ ದಾಖಲೆ (ಆರ್‌ಟಿಸಿ)",
}


def _parse_anomaly(anomaly: str) -> dict[str, str]:
    """Extract the (name, doc_type) pairs from a single anomaly string."""
    pairs: list[tuple[str, str]] = _NAME_ON_DOC_RE.findall(anomaly)
    parsed: dict[str, str] = {}
    for name, doc_type in pairs:
        parsed[doc_type] = name.strip()
    return parsed


def generate_mismatch_affidavit_metadata(
    case_id: str,
    anomalies: list[str],
) -> dict[str, Any]:
    """
    Build structured, bilingual affidavit metadata from the anomaly list.

    Returns a dict carrying:
      - case_id
      - has_mismatch: bool
      - declarant_aadhaar_name / declarant_ration_name (best-effort extraction)
      - mismatches: list of normalized {source_*, target_*} records
      - notary_sworn_text_en / notary_sworn_text_kn
      - affidavit_body_en / affidavit_body_kn
      - generated_at (ISO-8601 UTC)
    """
    generated_at: str = datetime.now(tz=timezone.utc).isoformat()

    mismatches: list[dict[str, str]] = []
    doc_name_map: dict[str, str] = {}

    for anomaly in anomalies:
        parsed: dict[str, str] = _parse_anomaly(anomaly)
        if len(parsed) < 2:
            continue
        # Sanitize every OCR-derived name before storing / embedding
        sanitized: dict[str, str] = {k: _sanitize_name(v) for k, v in parsed.items()}
        doc_name_map.update(sanitized)
        doc_types: list[str] = list(sanitized.keys())
        source_doc: str = doc_types[0]
        target_doc: str = doc_types[1]
        mismatches.append(
            {
                "source_doc_type": source_doc,
                "source_doc_label_en": _DOC_LABELS_EN.get(source_doc, source_doc),
                "source_doc_label_kn": _DOC_LABELS_KN.get(source_doc, source_doc),
                "source_name": sanitized[source_doc],
                "target_doc_type": target_doc,
                "target_doc_label_en": _DOC_LABELS_EN.get(target_doc, target_doc),
                "target_doc_label_kn": _DOC_LABELS_KN.get(target_doc, target_doc),
                "target_name": sanitized[target_doc],
                "raw_anomaly": anomaly,
            }
        )

    # Aadhaar is the authoritative identity; prefer its name as the declarant's
    # legal name and treat the other document's spelling as the variant.
    declarant_aadhaar_name: str = doc_name_map.get("aadhaar", "")

    # If Aadhaar was not one of the mismatched docs, fall back to the first name.
    if not declarant_aadhaar_name and doc_name_map:
        declarant_aadhaar_name = next(iter(doc_name_map.values()))

    # Pick the "other" document name in priority order (ration_card > bank_passbook >
    # land_record > anything else). This fixes the B10 bug where the passbook name was
    # silently used but still labelled as "ration name" when no ration anomaly existed.
    declarant_other_doc_type: str = ""
    declarant_ration_name: str = ""
    for preferred in _OTHER_DOC_PRIORITY:
        if preferred in doc_name_map:
            declarant_other_doc_type = preferred
            declarant_ration_name = doc_name_map[preferred]
            break
    if not declarant_ration_name:
        for k, v in doc_name_map.items():
            if k != "aadhaar":
                declarant_other_doc_type = k
                declarant_ration_name = v
                break

    other_label_en: str = _DOC_LABELS_EN.get(declarant_other_doc_type, "another document")
    other_label_kn: str = _DOC_LABELS_KN.get(declarant_other_doc_type, "ಮತ್ತೊಂದು ದಾಖಲೆ")

    notary_sworn_text_en: str = (
        f"I, {declarant_aadhaar_name or '[Declarant Name]'}, do hereby solemnly "
        f"affirm and declare that the names "
        f"\"{declarant_aadhaar_name or '[Aadhaar Name]'}\" (on Aadhaar Card) and "
        f"\"{declarant_ration_name or '[Other Document Name]'}\" (on {other_label_en}) "
        f"appearing on my official documents belong to one and the same person, "
        f"i.e. myself. The discrepancy is due to a clerical/spelling variation only. "
        f"I undertake that all such documents pertain to me and request the "
        f"concerned authorities to treat them as belonging to the same individual "
        f"for the purpose of welfare scheme enrolment."
    )

    notary_sworn_text_kn: str = (
        f"ನಾನು, {declarant_aadhaar_name or '[ಘೋಷಕರ ಹೆಸರು]'}, ಈ ಮೂಲಕ ಗಂಭೀರವಾಗಿ "
        f"ದೃಢೀಕರಿಸುತ್ತೇನೆ ಮತ್ತು ಘೋಷಿಸುತ್ತೇನೆ ಏನೆಂದರೆ, ನನ್ನ ಅಧಿಕೃತ ದಾಖಲೆಗಳಲ್ಲಿ "
        f"ಕಾಣಿಸಿಕೊಂಡಿರುವ \"{declarant_aadhaar_name or '[ಆಧಾರ್ ಹೆಸರು]'}\" (ಆಧಾರ್ ಕಾರ್ಡ್ ಮೇಲೆ) "
        f"ಮತ್ತು \"{declarant_ration_name or '[ಇತರ ದಾಖಲೆ ಹೆಸರು]'}\" ({other_label_kn} ಮೇಲೆ) "
        f"ಎಂಬ ಹೆಸರುಗಳು ಒಬ್ಬರೇ ವ್ಯಕ್ತಿಗೆ — ಅಂದರೆ ನನಗೆ — ಸೇರಿವೆ. ಈ ವ್ಯತ್ಯಾಸವು "
        f"ಕೇವಲ ಕ್ಲರಿಕಲ್/ಅಕ್ಷರ ದೋಷದಿಂದ ಉಂಟಾಗಿದೆ. ಕಲ್ಯಾಣ ಯೋಜನೆಯ ನೋಂದಣಿಗಾಗಿ ಈ "
        f"ಎಲ್ಲಾ ದಾಖಲೆಗಳನ್ನು ಒಬ್ಬರೇ ವ್ಯಕ್ತಿಯ ದಾಖಲೆಗಳೆಂದು ಪರಿಗಣಿಸಬೇಕೆಂದು "
        f"ಸಂಬಂಧಪಟ್ಟ ಅಧಿಕಾರಿಗಳನ್ನು ವಿನಂತಿಸುತ್ತೇನೆ."
    )

    affidavit_body_en: str = (
        "AFFIDAVIT OF NAME DISCREPANCY\n\n"
        "(To be executed on Non-Judicial Stamp Paper of Rs. 10/- and attested "
        "by a Notary Public / Gazetted Officer)\n\n"
        f"{notary_sworn_text_en}\n\n"
        "DEPONENT\n"
        "Verified at __________ on this _____ day of __________, 20____, that the "
        "contents of the above affidavit are true and correct to the best of my "
        "knowledge and belief."
    )

    affidavit_body_kn: str = (
        "ಹೆಸರು ವ್ಯತ್ಯಾಸದ ಪ್ರಮಾಣಪತ್ರ\n\n"
        "(ರೂ. 10/- ರ ನ್ಯಾಯೇತರ ಛಾಪಾ ಕಾಗದದ ಮೇಲೆ ಬರೆದು ನೋಟರಿ ಪಬ್ಲಿಕ್ / ಗೆಜೆಟೆಡ್ "
        "ಅಧಿಕಾರಿಯಿಂದ ದೃಢೀಕರಿಸಬೇಕು)\n\n"
        f"{notary_sworn_text_kn}\n\n"
        "ಘೋಷಕರು\n"
        "ಮೇಲಿನ ಪ್ರಮಾಣಪತ್ರದ ವಿಷಯಗಳು ನನ್ನ ತಿಳಿವಳಿಕೆ ಮತ್ತು ನಂಬಿಕೆಯ ಪ್ರಕಾರ ಸತ್ಯ ಮತ್ತು "
        "ಸರಿಯಾಗಿವೆ ಎಂದು __________ ನಲ್ಲಿ, 20____ ರ __________ ತಿಂಗಳ _____ ದಿನದಂದು "
        "ದೃಢೀಕರಿಸಲಾಗಿದೆ."
    )

    return {
        "case_id": case_id,
        "document_title_en": "Affidavit of Name Discrepancy",
        "document_title_kn": "ಹೆಸರು ವ್ಯತ್ಯಾಸದ ಪ್ರಮಾಣಪತ್ರ",
        "has_mismatch": len(mismatches) > 0,
        "declarant_aadhaar_name": declarant_aadhaar_name,
        "declarant_ration_name": declarant_ration_name,
        "declarant_other_doc_type": declarant_other_doc_type,
        "declarant_other_doc_label_en": other_label_en,
        "declarant_other_doc_label_kn": other_label_kn,
        "mismatches": mismatches,
        "stamp_paper_value_inr": 10,
        "jurisdiction": "Republic of India",
        "notary_sworn_text_en": notary_sworn_text_en,
        "notary_sworn_text_kn": notary_sworn_text_kn,
        "affidavit_body_en": affidavit_body_en,
        "affidavit_body_kn": affidavit_body_kn,
        "generated_at": generated_at,
    }
