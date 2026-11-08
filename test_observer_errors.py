"""A short hub outage must not raise an observer-error per chat; a persistent scan failure still does."""
import unittest
from unittest.mock import patch
import test_supervisor as fixtures

class ObserverErrors(fixtures.IsolatedSupervisorTest):
 setUp=fixtures.Tests.setUp
 tearDown=fixtures.Tests.tearDown
 def errors(self):return self.s.rows("SELECT * FROM events WHERE kind='observer-error'")
 def test_blip_is_silent_persistent_failure_is_reported_once_and_recovery_resets(self):
  with patch.object(self.s,'scan',side_effect=OSError('Connection refused')):
   self.s.reconcile();self.s.reconcile();self.assertEqual(self.errors(),[])      # short outage: no event
  with patch.object(self.s,'scan',return_value={}):self.s.reconcile()            # hub back: streak resets
  with patch.object(self.s,'scan',side_effect=OSError('Connection refused')):
   self.s.reconcile();self.s.reconcile();self.assertEqual(self.errors(),[])      # counting restarted
   self.s.reconcile();self.assertEqual(len(self.errors()),1)                     # third consecutive failure
   self.s.reconcile();self.s.reconcile();self.assertEqual(len(self.errors()),1)  # reported once per outage
  self.assertEqual(self.api.sent,[])
if __name__=='__main__':unittest.main()
