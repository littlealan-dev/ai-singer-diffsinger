from music21 import stream, note, bar, spanner

def create_ending_score():
    s = stream.Score()
    p = stream.Part()
    
    # Measure 1
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
    # 1st ending bracket covers m2
    rb1 = spanner.RepeatBracket(m2, number=1)
    # 2nd ending bracket covers m3
    rb2 = spanner.RepeatBracket(m3, number=2)
    
    p.append([m1, m2, m3, m4])
    p.insert(0, rb1)
    p.insert(0, rb2)
    
    s.append(p)
    return s

def test_expansion():
    s = create_ending_score()
    print("Original Score Structure:")
    for el in s.parts[0].recurse().notes:
         print(f"Measure {el.measureNumber}: {el.nameWithOctave}")

    print("\nExpanding Repeats...")
    try:
        s_expanded = s.expandRepeats()
    except Exception as e:
        print(f"Error expanding repeats: {e}")
        return

    print("Expanded Score Structure:")
    expected_measures = [1, 2, 1, 3, 4]
    actual_measures = []
    for el in s_expanded.parts[0].recurse().notes:
         print(f"Measure {el.measureNumber}: {el.nameWithOctave}")
         actual_measures.append(el.measureNumber)
         
    if actual_measures == expected_measures:
        print("SUCCESS: Measures match expected sequence [1, 2, 1, 3, 4]")
    else:
        print(f"FAILURE: Expected {expected_measures}, got {actual_measures}")

if __name__ == "__main__":
    test_expansion()
