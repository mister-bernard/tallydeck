import unittest,runpy,tempfile,json,time,os
from pathlib import Path
from unittest.mock import patch
m=runpy.run_path(str(Path(__file__).resolve().parents[1] / 'contrib' / 'tally-reap'),run_name='ram_reaper_test')
g=m['collect'].__globals__
class Safety(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def transcript(self,events):
  p=self.root/'rollout-test.jsonl';p.write_text('\n'.join(json.dumps({'type':'event_msg','payload':{'type':e}}) for e in events));return p
 def record(self):
  return dict(slug='fixture',attached=0,pids=[123],state='idle',telegraph=False,spawned=True,uuid='fixture-id',idle_h=4,mem_mb=100,harness='codex',cwd='/tmp',account='O',transcript='/tmp/fake')
 def verdict(self,r):
  with patch.dict(g,descendants=lambda pid:[],TB_PINNED=self.root/'pinned',TB_TASKS=self.root/'tasks'):
   return m['verdict'](r,3,50,set(),False)[0]
 def test_completed(self):self.assertTrue(m['codex_complete'](self.transcript(['task_started','task_complete'])))
 def test_new_turn(self):self.assertFalse(m['codex_complete'](self.transcript(['task_complete','task_started'])))
 def test_aborted(self):self.assertFalse(m['codex_complete'](self.transcript(['task_complete','turn_aborted'])))
 def test_missing(self):self.assertFalse(m['codex_complete'](self.root/'missing'))
 def test_protections(self):
  for changes in [dict(attached=1),dict(state='working'),dict(state='attention'),dict(uuid=''),dict(telegraph=True),dict(slug='main'),dict(spawned=False),dict(idle_h=2.99)]:
   with self.subTest(changes=changes):r=self.record();r.update(changes);self.assertFalse(self.verdict(r))
 def test_eligible(self):self.assertTrue(self.verdict(self.record()))
 def test_active_child(self):
  with patch.dict(g,descendants=lambda pid:[999]):self.assertFalse(m['verdict'](self.record(),3,50,set(),False)[0])
 def test_identity_changed(self):
  r=self.record();other=dict(r,uuid='new')
  with patch.dict(g,collect=lambda:[other]),patch('os.kill') as kill:
   self.assertFalse(m['reap'](r,0));kill.assert_not_called()
 def test_attachment_changed(self):
  r=self.record();other=dict(r,attached=1)
  with patch.dict(g,collect=lambda:[other]),patch('os.kill') as kill:
   self.assertFalse(m['reap'](r,0));kill.assert_not_called()
 def test_grace_never_force_closes(self):
  r=self.record();r['pids']=[os.getpid()]
  with patch.dict(g,collect=lambda:[r],verdict=lambda *a:(True,''),telegraph_sessions=lambda:set(),REAPED=self.root,LEDGER=self.root/'ledger',tmux=lambda *a:self.fail('must not kill tmux')),patch('os.kill') as kill:
   self.assertFalse(m['reap'](r,0));self.assertEqual(kill.call_count,1);self.assertTrue((self.root/'ledger').exists())
class Ownership(unittest.TestCase):
 def test_direct_fds_and_duplicates(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);fp=root/'rollout-fixture.jsonl';uid='11111111-1111-1111-1111-111111111111'
   fp.write_text(json.dumps({'type':'session_meta','payload':{'id':uid}})+'\n'+json.dumps({'type':'event_msg','payload':{'type':'task_complete'}})+'\n');os.utime(fp,(time.time()-14400,)*2)
   base=dict(tmux=lambda *a:'one\t0\t101',descendants=lambda p:[],is_agent=lambda p:True,fd_uuid=lambda p:'',proc_mem_mb=lambda p:100,proc_env=lambda p:{'TALLY_HARNESS':'codex','TALLY_SPAWNED':'1'},SPAWNED=root)
   with patch.dict(g,**base,rollout_fds=lambda p:{str(fp)}),patch('os.readlink',return_value='/fixture'):
    r=m['collect']()[0];self.assertEqual(r['uuid'],uid);self.assertEqual(r['state'],'idle')
   with patch.dict(g,**base,rollout_fds=lambda p:{str(fp),'/another/rollout.jsonl'}),patch('os.readlink',return_value='/fixture'):
    self.assertEqual(m['collect']()[0]['uuid'],'')
   base['tmux']=lambda *a:'one\t0\t101\ntwo\t0\t102'
   with patch.dict(g,**base,rollout_fds=lambda p:{str(fp)}),patch('os.readlink',return_value='/fixture'):
    self.assertTrue(all(not r['uuid'] for r in m['collect']()))
 def test_claude_cwd_never_guesses(self):
  with patch.dict(g,tmux=lambda *a:'one\t0\t101',descendants=lambda p:[],is_agent=lambda p:True,fd_uuid=lambda p:'',proc_mem_mb=lambda p:100,proc_env=lambda p:{'TALLY_HARNESS':'claude'},cwd_transcripts=lambda p:self.fail('must not guess by cwd')),patch('os.readlink',return_value='/fixture'):
   self.assertEqual(m['collect']()[0]['uuid'],'')
if __name__=='__main__':unittest.main()
