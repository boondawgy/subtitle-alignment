import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import analyze_alignment as align

class AlignmentTests(unittest.TestCase):
    def test_recovers_offset_with_misheard_words(self):
        phrases=['the rocket launch begins tonight', 'please bring the red suitcase', 'the train leaves before daylight', 'we should visit the old lighthouse']
        blocks=[]; samples=[]
        for i,phrase in enumerate(phrases):
            minute=i*6
            blocks.append(f'{i+1}\n00:{minute:02}:10,000 --> 00:{minute:02}:12,000\n{phrase}\n')
            heard=phrase.replace('red suitcase','blue suitcase')
            samples.append({'start_seconds':minute*60,'segments':[{'start_seconds':minute*60+13,'text':heard}]})
        result=align.analyze({'samples':samples},{'id':1,'name':'test','srt':'\n'.join(blocks)})
        self.assertEqual(result['status'],'analyzed')
        self.assertAlmostEqual(result['recommended_offset_seconds'],3)
        self.assertEqual(result['matching_sample_windows'],4)

    def test_unrelated_subtitles_do_not_get_confident_recommendation(self):
        result=align.analyze({'samples':[{'start_seconds':0,'segments':[{'start_seconds':5,'text':'the spaceship leaves tomorrow'}]}]}, {'srt':'1\n00:00:05,000 --> 00:00:07,000\nApples and oranges are delicious.'})
        self.assertEqual(result['status'],'insufficient evidence')
        self.assertIsNone(result['recommended_offset_seconds'])

if __name__=='__main__':unittest.main()
