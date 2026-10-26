import importlib.util
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('firefox_clone',Path(__file__).parent/'deploy/firefox/clone-profile.py')
clone=importlib.util.module_from_spec(spec);spec.loader.exec_module(clone)

class ProfileCloneTests(unittest.TestCase):
 def test_live_wal_snapshot_independent_files_no_sync_or_session_restore(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'daily';source.mkdir();dest=root/'agent/profile'
   db=sqlite3.connect(source/'cookies.sqlite');db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE cookie(value)');db.execute("INSERT INTO cookie VALUES('test')");db.commit()
   (source/'logins.json').write_text('{"encrypted":"fixture"}');(source/'lock').symlink_to('pid')
   (source/'sessionstore-backups').mkdir();(source/'sessionstore-backups/recovery.jsonlz4').write_text('tabs')
   (source/'signedInUser.json').write_text('sync-account');(source/'prefs.js').write_text('user_pref("services.sync.username", "fixture");\nuser_pref("identity.fxaccounts.uid", "fixture");\nuser_pref("browser.tabs.warnOnClose", true);\n')
   (source/'extensions').mkdir();(source/'extensions/test.xpi').write_bytes(b'extension')
   (source/'extensions.json').write_text(json.dumps({'path':str(source/'extensions/test.xpi'), 'uri':(source/'extensions/test.xpi').as_uri()}))
   (source/'pkcs11.txt').write_text('configdir='+str(source))
   (source/'addonStartup.json.lz4').write_bytes(b'cached absolute paths')
   (source/'external').symlink_to(source/'logins.json')
   original=(source/'prefs.js').read_bytes()
   with patch.object(Path,'home',return_value=root):result=clone.clone(source,dest)
   check=sqlite3.connect(dest/'cookies.sqlite')
   try:self.assertEqual(check.execute('SELECT value FROM cookie').fetchone()[0],'test')
   finally:check.close()
   self.assertFalse((dest/'cookies.sqlite-wal').exists());self.assertFalse((dest/'lock').exists());self.assertFalse((dest/'external').exists())
   self.assertFalse((dest/'signedInUser.json').exists());self.assertFalse((dest/'sessionstore-backups').exists())
   self.assertEqual((dest/'logins.json').read_bytes(),(source/'logins.json').read_bytes())
   (dest/'logins.json').write_text('changed');self.assertNotEqual((source/'logins.json').read_text(),'changed')
   self.assertNotEqual((source/'extensions/test.xpi').stat().st_ino,(dest/'extensions/test.xpi').stat().st_ino)
   self.assertIn(str(dest),(dest/'pkcs11.txt').read_text());self.assertNotIn(str(source),(dest/'extensions.json').read_text());self.assertNotIn(source.as_uri(),(dest/'extensions.json').read_text());self.assertFalse((dest/'addonStartup.json.lz4').exists())
   self.assertEqual(original,(source/'prefs.js').read_bytes());self.assertNotIn('services.sync.username',(dest/'prefs.js').read_text())
   self.assertIn('"marionette.port", 2829',(dest/'user.js').read_text());self.assertIn('"identity.fxaccounts.enabled", false',(dest/'user.js').read_text())
   self.assertEqual(dest.stat().st_mode&0o777,0o700);self.assertEqual((dest/'logins.json').stat().st_mode&0o777,0o600)
   self.assertGreater(result['sqliteSnapshots'],0);db.close()
 def test_exclusive_live_database_uses_stable_wal_copy_without_touching_source(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'locked.sqlite';dest=root/'copy.sqlite'
   db=sqlite3.connect(source);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA locking_mode=EXCLUSIVE');db.execute('CREATE TABLE entries(value)');db.execute("INSERT INTO entries VALUES('committed WAL')");db.commit()
   token=clone.fingerprint(source);wal=clone.fingerprint(Path(str(source)+'-wal'))
   with self.assertRaises(TimeoutError):clone.sqlite_snapshot(source,dest,deadline=.01)
   dest.unlink(missing_ok=True)
   clone.stable_sqlite_snapshot(source,dest)
   copied=sqlite3.connect(dest)
   try:self.assertEqual(copied.execute('SELECT value FROM entries').fetchone()[0],'committed WAL')
   finally:copied.close()
   self.assertEqual(clone.fingerprint(source),token);self.assertEqual(clone.fingerprint(Path(str(source)+'-wal')),wal)
   db.close()
 def test_firefox_custom_collation_is_preserved_and_check_limitation_explicit(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'firefox.sqlite';dest=root/'copy.sqlite'
   db=sqlite3.connect(source);db.create_collation('firefox_only',lambda a,b:(a>b)-(a<b));db.execute('CREATE TABLE names(value TEXT COLLATE firefox_only)');db.execute('CREATE INDEX ordered ON names(value)');db.commit();db.close()
   self.assertFalse(clone.sqlite_snapshot(source,dest))
   copied=sqlite3.connect(dest)
   try:self.assertIn('firefox_only',copied.execute("SELECT sql FROM sqlite_master WHERE name='names'").fetchone()[0])
   finally:copied.close()
 def test_existing_destination_and_overlapping_source_are_refused(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'daily';source.mkdir();dest=root/'agent';dest.mkdir();(dest/'work').write_text('keep')
   with self.assertRaises(FileExistsError):clone.clone(source,dest)
   self.assertEqual((dest/'work').read_text(),'keep')
   with self.assertRaises(ValueError):clone.clone(source,source/'inside')
 def test_failed_snapshot_never_publishes_half_profile(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'daily';source.mkdir();dest=root/'agent/profile'
   db=sqlite3.connect(source/'key4.db');db.execute('CREATE TABLE keys(value)');db.close()
   with patch.object(clone,'sqlite_snapshot',side_effect=RuntimeError('busy')):
    with self.assertRaises(RuntimeError):clone.clone(source,dest)
   self.assertFalse(dest.exists());self.assertEqual(list(dest.parent.iterdir()),[])

if __name__=='__main__':unittest.main()
