from __future__ import annotations
from typing import List, Optional, Sequence, TYPE_CHECKING
from .phoneme_logic_handler import PhonemeLogicHandler

if TYPE_CHECKING:
    from .phonemizer import Phonemizer


class EnglishPhonemeLogicHandler(PhonemeLogicHandler):
    """
    English-specific phoneme logic.
    """
    
    def distribute_slur(
        self, 
        phonemes: Sequence[str], 
        note_count: int, 
        phonemizer: Phonemizer
    ) -> Optional[List[List[str]]]:
        """
        English Slur Distribution Strategy.
        Propagates the primary vowel across all notes.
        Structure:
          - Note 0: Onset + Vowel
          - Middle Notes: Vowel only
          - Last Note: Vowel + Coda
        """
        # 1. Identify vowel positions
        is_vowel = [phonemizer.is_vowel(p) for p in phonemes]
        
        # Determine the "primary" vowel for the slur.
        # Simple heuristic: First vowel found.
        try:
            vowel_idx = is_vowel.index(True)
        except ValueError:
            # No vowel found? 
            # Fallback to default behavior (None) or handle it?
            # If we return None, standard logic applies (everything on first note usually).
            return None
            
        primary_vowel = phonemes[vowel_idx]
        
        # 2. Build the groups
        distribution: List[List[str]] = []
        
        # Note 0: Onset (0 to vowel_idx) + Vowel
        # If the word starts with a vowel, onset is empty, so just Vowel.
        onset_plus_vowel = list(phonemes[:vowel_idx+1])
        distribution.append(onset_plus_vowel)
        
        # Middle Notes (1 to N-2): Just the vowel
        # If note_count = 3 (0, 1, 2). Middle is index 1.
        for _ in range(1, note_count - 1):
            distribution.append([primary_vowel])
            
        # Last Note (index N-1): Vowel + Coda (vowel_idx+1 to end)
        
        if note_count == 1:
            # Special case: Slur of 1 note is just the word.
            # The loop above didn't run.
            return [list(phonemes)]
            
        # If we have at least 2 notes, we add the last note entry.
        coda_part = list(phonemes[vowel_idx+1:])
        last_note_phonemes = [primary_vowel] + coda_part
        distribution.append(last_note_phonemes)
        
        return distribution
