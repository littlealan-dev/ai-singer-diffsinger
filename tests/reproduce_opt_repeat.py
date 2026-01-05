from music21 import stream, note, bar, spanner

def create_ending_score():
    s = stream.Score()
    p = stream.Part()
    
    # Measure 1: Repeat Start
    m1 = stream.Measure(number=1)
    m1.append(note.Note("C4", type='quarter'))
    m1.leftBarline = bar.Repeat(direction='start')
    
    # Measure 2: 1st Ending
    m2 = stream.Measure(number=2)
    m2.append(note.Note("D4", type='quarter'))
    m2.rightBarline = bar.Repeat(direction='end')
    
    # Measure 3: 2nd Ending
    m3 = stream.Measure(number=3)
    m3.append(note.Note("E4", type='quarter'))
    
    # Measure 4
    m4 = stream.Measure(number=4)
    m4.append(note.Note("F4", type='quarter'))
    
    # Add brackets
    rb1 = spanner.RepeatBracket(m2, number=1)
    rb2 = spanner.RepeatBracket(m3, number=2)
    
    p.append([m1, m2, m3, m4])
    p.insert(0, rb1)
    p.insert(0, rb2)
    
    s.append(p)
    return s

def test_selective_skip_first_ending():
    s = create_ending_score()
    print("Original Score Structure:")
    for el in s.parts[0].recurse().notes:
         print(f"Measure {el.measureNumber}: {el.nameWithOctave}")

    print("\nModifying score to remove 1st ending and Repeats (Transformation: A[1]A[2] -> A[2])...")
    
    p = s.parts[0]
    
    # 1. Identify 1st ending measures
    brackets = list(p.spanners.getElementsByClass(spanner.RepeatBracket))
    first_ending_bracket = next((b for b in brackets if b.number == '1'), None)
    
    if first_ending_bracket:
        # Remove measures spanned by 1st ending
        for el in first_ending_bracket.getSpannedElements():
             if isinstance(el, stream.Measure):
                 p.remove(el)
        p.remove(first_ending_bracket)
        
    # 2. Identify 2nd ending bracket and just remove the bracket (keeping contents)
    second_ending_bracket = next((b for b in brackets if b.number == '2'), None)
    if second_ending_bracket:
        p.remove(second_ending_bracket)
        
    # 3. Clean up Repeat Barlines
    # We want to remove ALL repeat barlines to make it a straight pass?
    # Or just the ones associated with this loop?
    # For simplicity, let's remove all repeat barlines in the part ensuring a linear flow
    for m in p.getElementsByClass(stream.Measure):
        if isinstance(m.leftBarline, bar.Repeat):
            m.leftBarline = None # Or regular barline
        if isinstance(m.rightBarline, bar.Repeat):
            m.rightBarline = None

    print("Modified Score Structure (Linearized):")
    # recurse should now skip m2
    for el in s.parts[0].recurse().notes:
         print(f"Measure {el.measureNumber}: {el.nameWithOctave}")

    # No need to call expandRepeats() now as we manually expanded/linearized it.
    
    expected_measures = [1, 3, 4]
    actual_measures = []
    for el in s.parts[0].recurse().notes:
        actual_measures.append(el.measureNumber)
        
    if actual_measures == expected_measures:
        print("SUCCESS: Resulting score matches [1, 3, 4] (A -> 2nd Ending -> Continuation)")
    else:
        print(f"FAILURE: Got {actual_measures}")

if __name__ == "__main__":
    test_selective_skip_first_ending()
