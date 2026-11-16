import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import auto_fit_subtitles as auto

class FakeApi:
    def __init__(self, state, pages):
        self.state=state;self.pages=pages;self.downloaded=[];self.page_calls=[]
    def call(self, method, endpoint, payload=None):
        if endpoint=='/api/search':
            self.page_calls.append(payload['page'])
            return {'candidates':self.pages.get(payload['page'],[]),'total_pages':max(self.pages)}
        if endpoint=='/api/download':
            cid=payload['file_id'];self.downloaded.append(cid)
            self.state['track_history'].append({'id':cid,'name':'new','file':'/data/test.srt'})
            return {'ok':True}
        raise AssertionError(endpoint)

class AutoFitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        self.args=types.SimpleNamespace(state=root/'state.json', transcriptions=root/'transcriptions', data_dir=root, report_dir=root/'report', progress=root/'progress.json', target=95, max_tracks=10, max_new_downloads=None, dry_run=False)
        self.movie={'path':'/movies/Test.mp4','title':'Test','embedded_checked':True,'has_subtitles':False,'track_history':[{'id':1,'name':'old','file':'/data/old.srt'}]}
        self.totals={'attempts':0,'downloaded':0,'stopped':None};self.progress={'uncertain':{}}

    def test_interactive_limits_accept_custom_values(self):
        args=types.SimpleNamespace(target=None,max_tracks=None)
        with patch('builtins.input',side_effect=['92','6']):
            auto.configure_limits(args,True)
        self.assertEqual((args.target,args.max_tracks),(92,6))

    def test_defaults_and_explicit_flags(self):
        args=types.SimpleNamespace(target=None,max_tracks=4)
        with patch('builtins.input',return_value='') as prompt:
            auto.configure_limits(args,True)
        self.assertEqual((args.target,args.max_tracks),(96,4));self.assertEqual(prompt.call_count,1)
        args=types.SimpleNamespace(target=None,max_tracks=None)
        with patch('builtins.input') as prompt:
            auto.configure_limits(args,False)
        self.assertEqual((args.target,args.max_tracks),(96,10));prompt.assert_not_called()

    def test_invalid_interactive_limits_are_reprompted(self):
        args=types.SimpleNamespace(target=None,max_tracks=None)
        with patch('builtins.input',side_effect=['101','95','0','3']):
            auto.configure_limits(args,True)
        self.assertEqual((args.target,args.max_tracks),(95,3))

    def test_stops_immediately_when_new_track_reaches_95(self):
        api=FakeApi(self.movie,{1:[{'download_id':1},{'download_id':2},{'download_id':3}]})
        with patch.object(auto,'get_movie',return_value=self.movie), patch.object(auto,'completed_transcript',return_value=(Path('transcript'),{'transcription':{'status':'complete'}})), patch.object(auto,'score_movie',side_effect=[45,95]):
            outcome=auto.run_movie(api,self.movie,self.args,self.progress,{},self.totals)
        self.assertEqual(api.downloaded,[2]);self.assertIn('target reached',outcome);self.assertEqual(self.totals['downloaded'],1)
        self.assertEqual(self.progress['uncertain'][self.movie['path']],[])

    def test_ten_track_limit_includes_rejected_and_existing(self):
        self.movie['rejected_ids']=[2,3,4,5,6,7,8,9]
        self.movie['track_history'].append({'id':'user-trim','name':'custom','file':'/data/custom.srt'})
        api=FakeApi(self.movie,{1:[{'download_id':1},{'download_id':2}],2:[{'download_id':10},{'download_id':11}]})
        with patch.object(auto,'get_movie',return_value=self.movie),patch.object(auto,'completed_transcript',return_value=(Path('transcript'),{'samples':[]})),patch.object(auto,'score_movie',side_effect=[45,50]):
            result=auto.run_movie(api,self.movie,self.args,self.progress,{},self.totals)
        self.assertEqual(api.downloaded,[10]);self.assertEqual(api.page_calls,[1,2]);self.assertIn('10-track limit',result)

    def test_pagination_limit_tries_id_search_and_does_not_stop_batch(self):
        calls=[]
        def call(method,endpoint,payload):
            calls.append(payload.copy())
            if payload.get('imdb_id'):
                return {'candidates':[{'download_id':5}], 'total_pages':1}
            if payload['page']==1:
                return {'candidates':[], 'total_pages':30}
            raise auto.BatchError('OpenSubtitles returned 400: Pagination too deep')
        api=types.SimpleNamespace(call=call)
        meta={self.movie['path']:{'imdb_id':'tt1234567','year':2000,'imdb_year':2000}}
        gen=auto.search_candidates(api,self.movie,set(),meta)
        with patch.object(auto.time,'sleep'):
            self.assertEqual(next(gen)['download_id'],5)
            with self.assertRaises(auto.SearchIncomplete):next(gen)
        self.assertEqual([x.get('imdb_id') for x in calls],[None,None,'tt1234567'])

    def test_pagination_limit_returns_incomplete_movie_outcome(self):
        api=types.SimpleNamespace(call=lambda *args: (_ for _ in ()).throw(auto.BatchError('Pagination too deep')))
        with patch.object(auto,'get_movie',return_value=self.movie),patch.object(auto,'completed_transcript',return_value=(Path('transcript'),{'samples':[]})),patch.object(auto,'score_movie',return_value=25):
            result=auto.run_movie(api,self.movie,self.args,self.progress,{},self.totals)
        self.assertIn('Search was limited',result)
        self.assertEqual(self.totals['downloaded'],0)

    def test_two_unrelated_pages_skip_broad_search_then_try_imdb(self):
        calls=[]
        def call(method,endpoint,payload):
            calls.append(payload.copy())
            if payload.get('imdb_id'):
                return {'candidates':[{'download_id':5}], 'matched_count':1, 'total_pages':1}
            return {'candidates':[], 'matched_count':0, 'total_pages':200}
        api=types.SimpleNamespace(call=call)
        meta={self.movie['path']:{'imdb_id':'tt1234567'}}
        with patch.object(auto.time,'sleep'):
            gen=auto.search_candidates(api,self.movie,set(),meta)
            self.assertEqual(next(gen)['download_id'],5)
            with self.assertRaises(auto.SearchIncomplete): next(gen)
        self.assertEqual([(x['page'],x.get('imdb_id')) for x in calls],[(1,None),(2,None),(1,'tt1234567')])

    def test_matching_but_already_tried_pages_do_not_trigger_shortcut(self):
        calls=[]
        def call(method,endpoint,payload):
            page=payload['page'];calls.append(page)
            return {'candidates':[{'download_id':page if page<3 else 4}], 'matched_count':1, 'total_pages':3}
        with patch.object(auto.time,'sleep'):
            candidates=list(auto.search_candidates(types.SimpleNamespace(call=call),self.movie,{'1','2'},{}))
        self.assertEqual([x['download_id'] for x in candidates],[4]);self.assertEqual(calls,[1,2,3])

    def test_passed_movie_is_not_downloaded(self):
        self.movie['passed']=True
        api=FakeApi(self.movie,{1:[{'download_id':2}]})
        with patch.object(auto,'get_movie',return_value=self.movie),patch.object(auto,'completed_transcript') as transcript:
            result=auto.run_movie(api,self.movie,self.args,self.progress,{},self.totals)
        self.assertEqual(result,'no longer eligible');self.assertEqual(api.page_calls,[]);transcript.assert_not_called()

    def test_missing_transcript_spends_no_downloads(self):
        api=FakeApi(self.movie,{1:[{'download_id':2}]})
        with patch.object(auto,'get_movie',return_value=self.movie),patch.object(auto,'completed_transcript',return_value=(Path('none'),None)):
            result=auto.run_movie(api,self.movie,self.args,self.progress,{},self.totals)
        self.assertIn('no completed transcription',result);self.assertEqual(api.downloaded,[]);self.assertEqual(api.page_calls,[])

    def test_changed_transcription_invalidates_cached_score(self):
        transcript=self.args.data_dir/'transcript.json';transcript.write_text('{"samples": []}')
        track=self.args.data_dir/'track.srt';track.write_text('subtitle')
        self.movie['track_history']=[{'id':1,'name':'old','file':'/data/track.srt'}]
        self.args.report_dir.mkdir()
        (self.args.report_dir/'report.json').write_text(json.dumps({'movies':[{'movie_path':self.movie['path'],'transcript_sha256':'outdated','tracks':[{'track_id':1,'subtitle_sha256':'old','fit_score':100}]}]}))
        with patch.object(auto,'analyze',return_value={'track_id':1,'subtitle_sha256':'new','status':'analyzed','fit_score':42}) as analyze:
            self.assertEqual(auto.score_movie(self.movie,transcript,{'samples':[]},self.args),42)
            analyze.assert_called_once()

    def test_rejected_high_score_does_not_meet_target(self):
        transcript=self.args.data_dir/'transcript.json';transcript.write_text('{"samples": []}')
        for name in ('one.srt','two.srt'):(self.args.data_dir/name).write_text(name)
        self.movie['track_history']=[{'id':1,'name':'first','file':'/data/one.srt'},{'id':2,'name':'rejected','file':'/data/two.srt'}]
        self.movie['rejected_ids']=[2]
        def fake_analysis(transcript,track):
            return {'track_id':track['id'],'subtitle_sha256':track['sha256'],'status':'analyzed','fit_score':98 if track['id']==2 else 55}
        with patch.object(auto,'analyze',side_effect=fake_analysis):
            self.assertEqual(auto.score_movie(self.movie,transcript,{'samples':[]},self.args),55)

    def test_uncertain_candidate_is_not_retried(self):
        self.progress['uncertain'][self.movie['path']]=['2']
        api=FakeApi(self.movie,{1:[{'download_id':2},{'download_id':3}]})
        with patch.object(auto,'get_movie',return_value=self.movie),patch.object(auto,'completed_transcript',return_value=(Path('transcript'),{'samples':[]})),patch.object(auto,'score_movie',side_effect=[20,99]):
            auto.run_movie(api,self.movie,self.args,self.progress,{},self.totals)
        self.assertEqual(api.downloaded,[3])

if __name__=='__main__':unittest.main()
