
import copy, json, stat, sys, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import cmux_codex_watch as core
import ccc_native_processes as native
import ccc_guard_scope as scope

SID="44444444-4444-4444-8444-444444444444"
WID="11111111-1111-4111-8111-111111111111"
WIN="22222222-2222-4222-8222-222222222222"
PANE="33333333-3333-4333-8333-333333333333"
OTHER="55555555-5555-4555-8555-555555555555"

def tree(tty="ttyFake"):
 surface={"id":SID,"ref":"surface:1","type":"terminal","kind":"surface"}
 if tty is not None: surface["tty"]=tty
 return {"windows":[{"id":WIN,"ref":"window:1","workspaces":[{"id":WID,"ref":"workspace:1","panes":[{"id":PANE,"ref":"pane:1","surfaces":[surface]}]}]}]}

def row(pid=100,kind="claude"):
 return {"surface_id":SID,"environment_workspace_id":WID,"pid":pid,"birth":(100,1),"agent_kind":kind,"pgid":pid,"tpgid":pid,"tdev":10,"argv_fingerprint":"pin"}

class Client:
 def __init__(self,t,top=None,error=False): self.t=t; self.payload={} if top is None else top; self.error=error; self.calls=0
 def tree(self): return copy.deepcopy(self.t)
 def top_all(self):
  self.calls+=1
  if self.error: raise core.CmuxError("GUI unavailable")
  return self.payload
 def replay(self,*args,**kwargs): raise core.CmuxError("No viewport fixture; this test never sends input")

class AuditTests(unittest.TestCase):
 def audit(self,rows=None,top=None,tty="ttyFake",error=False,device=10,changed=False,entries=None,native_error=False):
  rows=[row()] if rows is None else rows
  client=Client(tree(tty),top,error)
  target={"surface_id":SID,"workspace_id":WID,"ref":"surface:1","title_snapshot":"Claude original","paused":False}
  config={"targets":[target],"workspace_rules":[],"claude_enabled":True}
  def current(pid):
   value=next((r for r in rows if r["pid"]==pid),None)
   return {**value,"birth":(100,2)} if value and changed else value
  with patch.object(scope,"scan",side_effect=RuntimeError("native unavailable") if native_error else None,return_value=rows) as scan, \
       patch.object(native,"display_process",side_effect=current), \
       patch.object(native,"_display_tty_device",return_value=device), \
       patch.object(core,"discover_all_targets",return_value=[]), \
       patch.object(core,"inspect_claude_process",return_value={"pid":100,"started_epoch":100}), \
       patch.object(core,"ClaudeHookSettingsManager"):
   if entries is None:
    result=core.audit_claude_surfaces(config,{},client,{"healthy":True})
   else:
    manager=MagicMock(); manager.__enter__.return_value=iter(entries)
    with patch("os.scandir",return_value=manager):
     result=core.audit_claude_surfaces(config,{},client,{"healthy":True})
  return result,client,scan.call_count

 def test_missing_gui_uses_exact_native_identity_once(self):
  result,client,calls=self.audit()
  self.assertEqual(result["live_count"],1)
  self.assertEqual(result["live"][0]["process_pid"],100)
  self.assertEqual(result["unobserved_registered"],[])
  self.assertEqual((client.calls,calls),(1,1))

 def test_gui_unavailable_is_native_only_not_process_exit(self):
  result,_,_=self.audit(error=True)
  self.assertEqual(result["live_count"],1)
  self.assertEqual(result["inventory"]["gui_status"],"unavailable")

 def test_explicit_incomplete_gui_snapshot_is_reported_partial(self):
  result,_,_=self.audit(top={"sample":{"enumeration_complete":False}})
  self.assertEqual(result["live_count"],1)
  self.assertEqual(result["inventory"]["gui_status"],"partial")
  self.assertEqual(result["inventory"]["gui_incomplete_workspaces"],[WID])

 def test_duplicate_claude_is_not_promoted(self):
  result,_,_=self.audit(rows=[row(),row(101)])
  self.assertEqual(result["live_count"],0)
  self.assertFalse(result["unobserved_registered"][0]["exit_confirmed"])

 def test_mixed_native_group_is_not_promoted(self):
  result,_,_=self.audit(rows=[row(),row(101,"codex")])
  self.assertEqual(result["live_count"],0)

 def test_changed_birth_is_rejected(self):
  result,_,_=self.audit(changed=True)
  self.assertEqual(result["live_count"],0)

 def test_wrong_tty_device_is_rejected(self):
  result,_,_=self.audit(device=11)
  self.assertEqual(result["live_count"],0)

 def test_nested_list_gui_conflict_is_preserved(self):
  foreign={"kind":"surface","id":SID,"workspace_id":WID,"processes":[{"kind":"process","pid":201,"cmux_surface_id":OTHER,"command":"claude"}]}
  result,_,_=self.audit(top={"outer":[[foreign]]})
  self.assertEqual(result["live_count"],0)

 def test_nested_populated_entry_not_lost_when_later_entry_empty(self):
  populated={"kind":"surface","id":SID,"workspace_id":WID,"processes":[{"kind":"process","pid":201,"command":"other"}]}
  empty={"kind":"surface","id":SID,"workspace_id":WID,"processes":[]}
  result,_,_=self.audit(top={"outer":[[populated],{"inner":[empty]}]})
  self.assertEqual(result["live_count"],0)

 def test_missing_tty_uses_only_unique_device_name(self):
  entry=SimpleNamespace(name="ttyRecovered",stat=lambda **kw:SimpleNamespace(st_mode=stat.S_IFCHR,st_rdev=10))
  result,_,_=self.audit(tty=None,entries=[entry])
  self.assertEqual(result["live_count"],1)

 def test_ambiguous_device_names_are_not_promoted(self):
  entry=lambda name:SimpleNamespace(name=name,stat=lambda **kw:SimpleNamespace(st_mode=stat.S_IFCHR,st_rdev=10))
  result,_,_=self.audit(tty=None,entries=[entry("ttyOne"),entry("ttyTwo")])
  self.assertEqual(result["live_count"],0)

 def test_missing_native_observation_is_not_exit(self):
  result,_,_=self.audit(native_error=True)
  self.assertEqual(result["live_count"],0)
  self.assertEqual(result["inventory"]["native_status"],"unavailable")
  self.assertEqual(result["unobserved_registered"][0]["observation_status"],"not_observed")
  self.assertFalse(result["unobserved_registered"][0]["exit_confirmed"])
  self.assertEqual(result["stale_registered"],result["unobserved_registered"])

if __name__=="__main__":
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(AuditTests)
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 summary={"tests_run":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),"successful":result.wasSuccessful(),"scope":"independent copy; no real processes, API, configuration, or input"}
 Path(__file__).with_name("TEST-RESULTS.json").write_text(json.dumps(summary,indent=2))
 raise SystemExit(0 if result.wasSuccessful() else 1)
