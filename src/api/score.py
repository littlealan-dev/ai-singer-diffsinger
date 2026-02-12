"""
Score parsing and modification APIs.
"""

import dataclasses
import json
import math
import logging
import zipfile
from xml.etree import ElementTree
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.mcp.logging_utils import get_logger, summarize_payload

logger = get_logger(__name__)

# Try to import RestrictedPython for sandboxing, fall back to unsafe exec
try:
    from RestrictedPython import compile_restricted
    from RestrictedPython.Guards import safe_builtins
    HAS_RESTRICTED_PYTHON = True
except ImportError:
    HAS_RESTRICTED_PYTHON = False
    logger.warning("RestrictedPython not installed. modify_score will use unsafe exec.")

from src.musicxml.parser import parse_musicxml_with_summary
from src.api.voice_parts import analyze_score_voice_parts


def parse_score(
    file_path: Union[str, Path],
    *,
    part_id: Optional[str] = None,
    part_index: Optional[int] = None,
    verse_number: Optional[str | int] = None,
    expand_repeats: bool = False,
) -> Dict[str, Any]:
    """
    Parse a MusicXML file into a JSON-serializable score dict.
    
    Args:
        file_path: Path to MusicXML file (.xml or .mxl)
        part_id: Specific part ID to extract (deprecated; full score is always parsed)
        part_index: Specific part index to extract (deprecated; full score is always parsed)
        verse_number: Lyric verse number to select (optional)
        expand_repeats: If True, expand repeat signs into linear sequence
        
    Returns:
        Score as a JSON-serializable dict with structure:
        {
            "title": str | None,
            "tempos": [...],
            "parts": [{"part_id": ..., "part_name": ..., "notes": [...]}],
            "score_summary": {...}
        }
    """
    if logger.isEnabledFor(logging.DEBUG):
        # Avoid heavy payload logs unless debug is enabled.
        logger.debug(
            "parse_score input=%s",
            summarize_payload(
                {
                    "file_path": str(file_path),
                    "part_id": part_id,
                    "part_index": part_index,
                    "verse_number": verse_number,
                    "expand_repeats": expand_repeats,
                }
            ),
        )
    # Delegate parsing to the MusicXML adapter, keeping rests for alignment.
    score_data, score_summary = parse_musicxml_with_summary(
        file_path,
        part_id=None,
        part_index=None,
        verse_number=verse_number,
        lyrics_only=False,
        keep_rests=True,
    )
    
    # Convert dataclass to dict for JSON serialization.
    score_dict = dataclasses.asdict(score_data)
    
    # Add structure placeholder (to be populated when parser supports it).
    score_dict["structure"] = {
        "repeats": [],
        "endings": [],
        "jumps": [],
    }
    score_dict["score_summary"] = score_summary
    score_dict["source_musicxml_path"] = str(Path(file_path).resolve())
    if part_id is not None or part_index is not None:
        score_dict["requested_part_id"] = part_id
        score_dict["requested_part_index"] = part_index
    score_dict["voice_part_signals"] = analyze_score_voice_parts(
        score_dict,
        verse_number=verse_number,
    )
    score_dict["voice_part_signals"]["measure_staff_voice_map"] = _build_measure_staff_voice_map(
        Path(file_path)
    )
    
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("parse_score output=%s", summarize_payload(score_dict))
    return score_dict


def _read_musicxml_content(path: Path) -> str:
    """Read MusicXML content from .xml or .mxl files."""
    if path.suffix.lower() != ".mxl":
        return path.read_text(encoding="utf-8", errors="replace")
    with zipfile.ZipFile(path) as archive:
        xml_name = _find_mxl_xml(archive)
        xml_bytes = archive.read(xml_name)
    return xml_bytes.decode("utf-8", errors="replace")


def _find_mxl_xml(archive: zipfile.ZipFile) -> str:
    """Find the referenced XML file inside an MXL archive."""
    try:
        container_bytes = archive.read("META-INF/container.xml")
    except KeyError:
        return _first_mxl_xml(archive)
    try:
        root = ElementTree.fromstring(container_bytes)
    except ElementTree.ParseError:
        return _first_mxl_xml(archive)
    for elem in root.iter():
        if elem.tag.endswith("rootfile"):
            full_path = elem.attrib.get("full-path")
            if full_path and full_path in archive.namelist():
                return full_path
    return _first_mxl_xml(archive)


def _first_mxl_xml(archive: zipfile.ZipFile) -> str:
    """Return the first XML entry found in an MXL archive."""
    candidates = [
        name
        for name in archive.namelist()
        if name.lower().endswith(".xml") and not name.startswith("META-INF/")
    ]
    if not candidates:
        raise ValueError("No MusicXML file found in archive.")
    return candidates[0]


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _build_measure_staff_voice_map(path: Path) -> List[Dict[str, Any]]:
    """Build per-measure staff/voice presence and lyric attachment map."""
    content = _read_musicxml_content(path)
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Invalid MusicXML: {exc}") from exc

    part_name_by_id: Dict[str, Optional[str]] = {}
    for elem in root.iter():
        if _local_tag(elem.tag) != "score-part":
            continue
        part_id = elem.attrib.get("id")
        if not part_id:
            continue
        part_name = None
        for child in elem:
            if _local_tag(child.tag) == "part-name":
                part_name = (child.text or "").strip() or None
                break
        part_name_by_id[part_id] = part_name

    def _sv_sort_key(staff: str, voice: str) -> tuple:
        def _num_or_str(value: str) -> Union[int, str]:
            return int(value) if value.isdigit() else value
        return (_num_or_str(staff), _num_or_str(voice))

    def _sv_list(values: set[tuple[str, str]]) -> List[Dict[str, str]]:
        return [
            {"staff": staff, "voice": voice}
            for staff, voice in sorted(values, key=lambda sv: _sv_sort_key(sv[0], sv[1]))
        ]

    def _note_has_lyric(note_elem: ElementTree.Element) -> bool:
        for child in note_elem:
            if _local_tag(child.tag) != "lyric":
                continue
            for lyric_child in child.iter():
                tag = _local_tag(lyric_child.tag)
                if tag == "text" and (lyric_child.text or "").strip():
                    return True
                if tag in {"syllabic", "extend", "elision", "humming", "laughing"}:
                    return True
        return False

    parts: List[Dict[str, Any]] = []
    for part in root.iter():
        if _local_tag(part.tag) != "part":
            continue
        part_id = part.attrib.get("id")
        measures: List[Dict[str, Any]] = []
        measure_index = 0
        for measure in part:
            if _local_tag(measure.tag) != "measure":
                continue
            measure_index += 1
            measure_number = measure.attrib.get("number") or str(measure_index)
            all_svs: set[tuple[str, str]] = set()
            lyric_svs: set[tuple[str, str]] = set()
            for note in measure:
                if _local_tag(note.tag) != "note":
                    continue
                is_rest = any(_local_tag(child.tag) == "rest" for child in note)
                if is_rest:
                    continue
                voice = "1"
                staff = "1"
                for child in note:
                    tag = _local_tag(child.tag)
                    if tag == "voice" and child.text:
                        voice = child.text.strip() or voice
                    elif tag == "staff" and child.text:
                        staff = child.text.strip() or staff
                all_svs.add((staff, voice))
                if _note_has_lyric(note):
                    lyric_svs.add((staff, voice))
            measures.append(
                {
                    "measure_number": measure_number,
                    "all_svs": _sv_list(all_svs),
                    "lyric_svs": _sv_list(lyric_svs),
                }
            )
        parts.append(
            {
                "part_id": part_id,
                "part_name": part_name_by_id.get(part_id),
                "measures": measures,
            }
        )
    return parts


# Safe builtins for modify_score
SAFE_BUILTINS = {
    "len": len,
    "range": range,
    "enumerate": enumerate,
    "min": min,
    "max": max,
    "round": round,
    "abs": abs,
    "sorted": sorted,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "True": True,
    "False": False,
    "None": None,
    "sum": sum,
    "zip": zip,
    "map": map,
    "filter": filter,
    "any": any,
    "all": all,
}


def _default_getattr(obj, name):
    """Default getattr guard - allows attribute access."""
    return getattr(obj, name)


def _default_getitem(obj, key):
    """Default getitem guard - allows indexing/subscripting."""
    return obj[key]


def _default_getiter(obj):
    """Default getiter guard - allows iteration."""
    return iter(obj)


# Create the full restricted namespace with guards
def _create_restricted_namespace(score):
    """Create namespace with RestrictedPython guards."""
    import math
    namespace = {
        "__builtins__": SAFE_BUILTINS,
        "score": score,
        "math": math,
        # RestrictedPython guards for iteration and item access
        "_getattr_": _default_getattr,
        "_getitem_": _default_getitem,
        "_getiter_": _default_getiter,
        "_iter_unpack_sequence_": lambda *args: args,
        "_write_": lambda x: x,  # Allow writing to objects.
    }
    return namespace



def modify_score(score: Dict[str, Any], code: str) -> Dict[str, Any]:
    """
    Execute Python code to modify the score JSON.
    
    The code runs in a sandboxed environment with access to:
    - `score`: The score dict (mutable)
    - `math`: Python math module
    - Basic builtins: len, range, enumerate, min, max, round, sorted, etc.
    
    The code does NOT have access to:
    - File I/O, network, imports, subprocess
    
    Args:
        score: Score JSON dict (will be modified in place)
        code: Python code string
        
    Returns:
        Modified score dict
        
    Raises:
        SyntaxError: If code has syntax errors
        Exception: If code raises an exception during execution
        
    Example:
        modify_score(score, '''
        for part in score['parts']:
            for note in part['notes']:
                if note['pitch_midi']:
                    note['pitch_midi'] += 12
        ''')
    """
    if logger.isEnabledFor(logging.DEBUG):
        # Log inputs sparsely to avoid leaking large scores or code.
        logger.debug(
            "modify_score input=%s",
            summarize_payload({"score": score, "code": code}),
        )
    if HAS_RESTRICTED_PYTHON:
        # Use restricted execution with explicit guards.
        try:
            byte_code = compile_restricted(code, "<modify_score>", "exec")
        except SyntaxError as e:
            raise SyntaxError(f"Invalid code: {e}")
        
        if byte_code is None:
            raise SyntaxError("Code compilation failed")
        
        namespace = _create_restricted_namespace(score)
        exec(byte_code, namespace)
    else:
        # Fallback: unrestricted execution (not recommended for production).
        namespace = {
            "__builtins__": SAFE_BUILTINS,
            "score": score,
            "math": math,
        }
        exec(code, namespace)
    
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("modify_score output=%s", summarize_payload(score))
    return score
