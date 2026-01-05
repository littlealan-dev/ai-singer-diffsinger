from music21 import stream, note, bar, repeat

def create_ds_al_coda_score():
    s = stream.Score()
    p = stream.Part()
    
    # Measure 1: Start
    m1 = stream.Measure(number=1)
    m1.append(note.Note("C4", type='quarter'))
    
    # Measure 2: Segno
    m2 = stream.Measure(number=2)
    m2.append(repeat.Segno())
    m2.append(note.Note("D4", type='quarter'))
    
    # Measure 3: To Coda
    m3 = stream.Measure(number=3)
    m3.append(note.Note("E4", type='quarter'))
    m3.append(repeat.Coda()) # Acts as "To Coda" marker when placed here? 
    # Actually, music21 is picky about placement. usually it's a text expression "To Coda" 
    # coupled with a Coda symbol somewhere. 
    # Let's use the RepeatExpression objects if possible or just standard markers.
    
    # Let's try explicit RepeatExpression commands
    # m3.rightBarline = bar.Barline(style='double') # Visual
    # We need a "To Coda" jump.
    
    # Measure 4: D.S. al Coda
    m4 = stream.Measure(number=4)
    m4.append(note.Note("F4", type='quarter'))
    # D.S. al Coda usually at the end of this measure
    m4.append(repeat.DalSegnoAlCoda())
    
    # Measure 5: Coda
    m5 = stream.Measure(number=5)
    m5.append(repeat.Coda()) # The actual Coda section start
    m5.append(note.Note("G4", type='quarter'))
    
    p.append([m1, m2, m3, m4, m5])
    s.append(p)
    return s

def test_ds_coda():
    s = create_ds_al_coda_score()
    print("Original Score Structure:")
    for el in s.parts[0].recurse().notes:
         print(f"Measure {el.measureNumber}: {el.nameWithOctave}")

    print("\nExpanding Repeats (Expert D.S. al Coda)...")
    # Expected: 
    # 1. m1 (C4)
    # 2. m2 (D4) - Segno
    # 3. m3 (E4)
    # 4. m4 (F4) - D.S. al Coda -> Jump to Segno (m2)
    # 5. m2 (D4)
    # 6. m3 (E4) - To Coda -> Jump to Coda (m5)
    # 7. m5 (G4)
    
    try:
        s_expanded = s.expandRepeats()
    except Exception as e:
        print(f"Error expanding repeats: {e}")
        return

    print("Expanded Score Structure:")
    expected_notes = ["C4", "D4", "E4", "F4", "D4", "E4", "G4"]
    actual_notes = []
    for el in s_expanded.parts[0].recurse().notes:
         print(f"Measure {el.measureNumber}: {el.nameWithOctave}")
         actual_notes.append(el.nameWithOctave)
         
    if actual_notes == expected_notes:
        print("SUCCESS: Expanded sequence matches D.S. al Coda structure.")
    else:
        print(f"FAILURE: Expected {expected_notes}, got {actual_notes}")

if __name__ == "__main__":
    test_ds_coda()
