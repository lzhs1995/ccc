"""Bounded retirement of an unreadable recycled PID without weakening UNKNOWN."""
import subprocess
import unittest
from unittest.mock import patch

import ccc_compact_guard as guard
import ccc_codex_queue as native

PID=32788
OLD=[1792489615,279153]
RECORDS=[{"pid":PID,"birth":OLD}]
def inventory(kind,flavor,buffer,size):
 if buffer is None:return 4
 buffer[0]=PID;return 4

def response(text="32788 Sun Oct 25 17:44:14 2026\n",rc=0,err=""):
 return subprocess.CompletedProcess([],rc,text,err)

class HistoricalPidRegression(unittest.TestCase):
 def setUp(self):
  clock=patch.object(guard.time,"time",return_value=1792963200)
  clock.start()
  self.addCleanup(clock.stop)
 def retirement(self,birth=None,responses=None):
  with patch.object(native,"_proc_listpids",side_effect=inventory), patch("ccc_guard_scope.birth",side_effect=birth if isinstance(birth,list) and not all(type(x) is int for x in birth) else None,return_value=birth), patch("subprocess.run",side_effect=responses or [response()]*4) as ps:
   guard._retired_generations(RECORDS)
   return ps.call_args_list
 def test_new_root_pid_generation_can_be_retired(self):
  calls=self.retirement()
  self.assertEqual(len(calls),4)
  self.assertTrue(all(c.args[0][0]=="/bin/ps" and c.kwargs["timeout"]==1 and c.kwargs["env"]["TZ"]=="UTC" for c in calls))
 def test_same_generation_remains_live(self):
  with self.assertRaisesRegex(ValueError,"still live"):
   self.retirement(birth=OLD)
 def test_reused_pid_exact_birth_still_works_without_ps(self):
  self.assertEqual(self.retirement(birth=[1792940000,100]),[])
 def test_same_coarse_second_cannot_retire_microsecond_unknown(self):
  import datetime
  text=str(PID)+" "+datetime.datetime.fromtimestamp(OLD[0],datetime.timezone.utc).strftime("%a %b %d %H:%M:%S %Y")+"\n"
  with self.assertRaisesRegex(ValueError,"query uncertain"):
   self.retirement(responses=[response(text)]*4)
 def test_earlier_start_second_cannot_retire_contradictory_generation(self):
  import datetime
  text=str(PID)+" "+datetime.datetime.fromtimestamp(OLD[0]-10,datetime.timezone.utc).strftime("%a %b %d %H:%M:%S %Y")+"\n"
  with self.assertRaisesRegex(ValueError,"query uncertain"):
   self.retirement(responses=[response(text)]*4)
 def test_changing_libproc_is_not_overridden(self):
  with self.assertRaisesRegex(ValueError,"query uncertain"):
   self.retirement(birth=[None,[1792940000,100]])
 def test_changing_ps_is_unknown(self):
  with self.assertRaisesRegex(ValueError,"query uncertain"):
   self.retirement(responses=[response(),response("32788 Sun Oct 25 17:44:15 2026\n")])
 def test_untrusted_ps_output_is_unknown(self):
  for value in [response(""),response("malformed"),response(rc=1),response(err="denied"),response("9999 Sun Oct 25 17:44:14 2026\n"),response("32788 Sun Feb 31 17:44:14 2026\n"),response("32788 Sun Oct 25 17:44:14 2099\n")]:
   with self.subTest(value=value):
    with self.assertRaisesRegex(ValueError,"query uncertain"):
     self.retirement(responses=[value]*4)
 def test_ps_timeout_is_unknown(self):
  with self.assertRaisesRegex(ValueError,"query uncertain"):
   self.retirement(responses=[subprocess.TimeoutExpired("/bin/ps",1)])
 def test_inventory_uncertainty_remains_unknown(self):
  with patch.object(native,"_proc_listpids",return_value=0):
   with self.assertRaisesRegex(ValueError,"inventory unavailable"):
    guard._retired_generations(RECORDS)

if __name__=="__main__":unittest.main()
