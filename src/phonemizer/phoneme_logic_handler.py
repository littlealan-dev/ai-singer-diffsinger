from __future__ import annotations
from typing import List, Optional, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from .phonemizer import Phonemizer


class PhonemeLogicHandler:
    """
    Interface for language-specific phoneme logic.
    Subclasses should implement specific behaviors (e.g. slur distribution)
    for a given language.
    """
    
    def distribute_slur(
        self, 
        phonemes: Sequence[str], 
        note_count: int, 
        phonemizer: Phonemizer
    ) -> Optional[List[List[str]]]:
        """
        Distribute phonemes across notes for a slur.
        
        Args:
            phonemes: List of phonemes in the slur.
            note_count: Number of notes in the slur.
            phonemizer: The phonemizer instance.
            
        Returns:
            A list of phoneme lists (one per note) if a specialized strategy exists.
            Returns None to indicate that the default fallback behavior should be used.
        """
        return None


import importlib

def get_phoneme_logic_handler(language: str) -> PhonemeLogicHandler:
    """Factory to get the logic handler for a language."""
    # Try to load module: .phoneme_logic_handler_{language}
    module_name = f".phoneme_logic_handler_{language}"
    package = __package__ or "src.phonemizer"
    
    try:
        module = importlib.import_module(module_name, package=package)
    except ImportError:
        # No specific handler found for this language
        return PhonemeLogicHandler()
    
    # Expected class name: {Language}PhonemeLogicHandler
    # e.g. en -> EnglishPhonemeLogicHandler
    # We can try to guess the class name or just look for a subclass in the module.
    # Simple convention: Capitalize language code? NO, language codes are small.
    # Let's verify the module content.
    
    # Convention: module has a class named {Language}PhonemeLogicHandler ?
    # Better: The module should export a specific function or class.
    # Let's inspect the module for a subclass of PhonemeLogicHandler.
    
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type) 
            and issubclass(attr, PhonemeLogicHandler) 
            and attr is not PhonemeLogicHandler
        ):
            return attr()
            
    return PhonemeLogicHandler()
