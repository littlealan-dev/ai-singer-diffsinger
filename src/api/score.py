"""
Score parsing and modification APIs.
"""

import dataclasses
import json
import math
import logging
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
        part_id: Specific part ID to extract (optional)
        part_index: Specific part index to extract (optional)
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
        part_id=part_id,
        part_index=part_index,
        verse_number=verse_number,
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
    
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("parse_score output=%s", summarize_payload(score_dict))
    return score_dict


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
