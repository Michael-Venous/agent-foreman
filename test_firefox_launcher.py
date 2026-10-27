"""Isolated-profile launcher regression coverage; never runs real Firefox/systemctl."""
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).parent/'deploy/firefox/firefox-agent-browser'
loader = importlib.machinery.SourceFileLoader('firefox_launcher', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


class FirefoxLauncherTests(unittest.TestCase):
    def setUp(self):
        self.profile_guard = patch.object(launcher, 'require_profile')
        self.profile_guard.start()
        self.addCleanup(self.profile_guard.stop)
        self.mode_guard = patch.object(launcher, 'prepare_mode')
        self.mode_guard.start()
        self.addCleanup(self.mode_guard.stop)

    def test_normal_desktop_passes_urls_and_private_flags_without_automation(self):
        with patch.object(launcher, 'unit_state', return_value=('inactive', 0)), patch.object(launcher.subprocess, 'run') as run:
            args = ['--private-window', 'https://example.test/?a=1&b=2']
            self.assertEqual(launcher.execute('desktop', args), ['/usr/bin/firefox', *args])
            run.assert_not_called()

    def test_cleanup_never_stops_graphical_firefox_outside_headless_unit(self):
        with patch.object(launcher, 'unit_state', return_value=('inactive', 0)), patch.object(launcher.subprocess, 'run') as run:
            self.assertIsNone(launcher.execute('finish', []));run.assert_not_called()

    def test_idle_agent_service_stops_before_agent_gui_and_waits_for_exit(self):
        with patch.object(launcher, 'unit_state', side_effect=[('active', 123), ('active', 123), ('inactive', 0)]), \
             patch.object(launcher, 'identity', side_effect=['boot-start', 'boot-start', None]), \
             patch.object(launcher, 'socket_states', return_value=['0A', '06']), patch.object(launcher.subprocess, 'run') as run:
            self.assertEqual(launcher.execute('gui', ['--new-window']), ['/usr/bin/firefox', '--marionette', '--no-remote', '-profile', str(launcher.PROFILE), '--new-window'])
            run.assert_called_once_with(['systemctl', '--user', 'stop', launcher.UNIT], check=True, timeout=30)

    def test_connected_controller_refuses_desktop_gui_and_cleanup(self):
        for command in ('gui', 'finish', 'stop'):
            with self.subTest(command=command), patch.object(launcher, 'unit_state', return_value=('active', 123)), \
                 patch.object(launcher, 'identity', return_value='boot-start'), \
                 patch.object(launcher, 'socket_states', return_value=['0A', '01']), patch.object(launcher.subprocess, 'run') as run:
                with self.assertRaisesRegex(launcher.Busy, 'agent is controlling'):
                    launcher.execute(command, [])
                run.assert_not_called()

    def test_late_controller_pid_replacement_and_service_change_do_not_stop(self):
        for states, identities, sockets in [
            ([('active', 123)]*2, ['a', 'a'], [['0A'], ['0A', '01']]),
            ([('active', 123)]*2, ['a', 'b'], [['0A']]),
            ([('active', 123), ('active', 999)], ['a'], [['0A']]),
        ]:
            with self.subTest(states=states, identities=identities), \
                 patch.object(launcher, 'unit_state', side_effect=states), patch.object(launcher, 'identity', side_effect=identities), \
                 patch.object(launcher, 'socket_states', side_effect=sockets), patch.object(launcher.subprocess, 'run') as run:
                with self.assertRaisesRegex(launcher.Busy, 'changed during handoff'):
                    launcher.execute('gui', [])
                run.assert_not_called()

    def test_stop_failure_or_surviving_profile_owner_prevents_second_launch(self):
        with patch.object(launcher, 'unit_state', return_value=('active', 123)), \
             patch.object(launcher, 'identity', return_value='a'), patch.object(launcher, 'socket_states', return_value=['0A']), \
             patch.object(launcher.subprocess, 'run', side_effect=subprocess.TimeoutExpired('stop', 30)):
            with self.assertRaises(subprocess.TimeoutExpired):launcher.execute('gui', [])
        with patch.object(launcher, 'unit_state', side_effect=[('active', 123)]*2+[('inactive', 0)]), \
             patch.object(launcher, 'identity', return_value='a'), patch.object(launcher, 'socket_states', return_value=['0A']), \
             patch.object(launcher.subprocess, 'run'):
            with self.assertRaisesRegex(launcher.Busy, 'not exited'):launcher.execute('gui', [])

    def test_start_reuses_listener_without_connecting_or_starting(self):
        with patch.object(launcher, 'socket_states', return_value=['0A', '01']), patch.object(launcher, 'endpoint_isolated', return_value=True), patch.object(launcher.subprocess, 'run') as run:
            self.assertIsNone(launcher.execute('start', []));run.assert_not_called()

    def test_start_refuses_normal_locked_profile_instead_of_hidden_conflicting_browser(self):
        with patch.object(launcher, 'socket_states', return_value=[]), patch.object(launcher, 'unit_state', return_value=('inactive', 0)), \
             patch.object(launcher, 'agent_profile_open', return_value=True), patch.object(launcher.subprocess, 'run') as run:
            with self.assertRaisesRegex(launcher.Busy, 'already open or locked'):launcher.execute('start', [])
            run.assert_not_called()

    def test_start_still_supports_explicit_headless_work_after_profile_is_closed(self):
        with patch.object(launcher, 'socket_states', return_value=[]), patch.object(launcher, 'unit_state', return_value=('inactive', 0)), \
             patch.object(launcher, 'agent_profile_open', return_value=False), patch.object(launcher.subprocess, 'run') as run:
            self.assertIsNone(launcher.execute('start', []))
            run.assert_called_once_with(['systemctl', '--user', 'start', launcher.UNIT], check=True, timeout=30)

    def test_passive_socket_inspection_handles_ipv4_ipv6_and_only_local_port(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'net').mkdir()
            (root/'net/tcp').write_text('header\n0: 0100007F:0B0D 00000000:0000 0A\n1: 0100007F:AAAA 0100007F:0B0D 01\n')
            (root/'net/tcp6').write_text('header\n2: 00000000000000000000000001000000:0B0D 00000000000000000000000001000000:AAAA 01\n')
            self.assertEqual(launcher.socket_states(root), ['0A', '01'])
            (root/'net/tcp6').unlink();self.assertEqual(launcher.socket_states(root), ['0A'])
            (root/'net/tcp').unlink()
            with self.assertRaises(FileNotFoundError):launcher.socket_states(root)

    def test_profile_identity_verifies_mode_profile_uid_and_pid_start(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'123';folder.mkdir()
            # comm may contain spaces/parentheses; starttime is field22.
            (folder/'stat').write_text('123 (firefox (headless)) S '+' '.join(['0']*18+['456']))
            (folder/'cmdline').write_bytes(b'/usr/bin/firefox\0--headless\0--marionette\0-profile\0'+str(launcher.PROFILE).encode()+b'\0')
            self.assertEqual(launcher.identity(123,root), '456')
            (folder/'cmdline').write_bytes(b'/usr/bin/firefox\0--marionette\0')
            with self.assertRaisesRegex(launcher.Busy, 'expected isolated headless'):launcher.identity(123,root)
            self.assertIsNone(launcher.identity(999,root))

    def test_stale_local_lock_is_allowed_without_removing_any_lock(self):
        with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'PROFILE', Path(td)), patch.object(launcher, 'profile_process_open', return_value=False), patch.object(launcher.os, 'kill', side_effect=ProcessLookupError):
            profile = Path(td)
            (profile/'.parentlock').touch()
            (profile/'lock').symlink_to('127.0.1.1:+987654')
            before = (profile/'lock').lstat().st_ino
            self.assertFalse(launcher.agent_profile_open())
            self.assertEqual((profile/'lock').readlink(), Path('127.0.1.1:+987654'))
            self.assertEqual((profile/'lock').lstat().st_ino, before)
            self.assertTrue((profile/'.parentlock').exists())

    def test_live_or_uninspectable_pid_and_unknown_hosts_block(self):
        for target, failure in [('127.0.1.1:+1234', None), ('127.0.1.1:+1234', PermissionError()), ('192.0.2.1:+1234', ProcessLookupError()), ('unknown:+1234', ProcessLookupError()), ('127.0.0.1:+0', ProcessLookupError())]:
            with self.subTest(target=target, failure=failure), tempfile.TemporaryDirectory() as td, patch.object(launcher, 'PROFILE', Path(td)), patch.object(launcher.os, 'kill', side_effect=failure):
                (Path(td)/'.parentlock').touch()
                (Path(td)/'lock').symlink_to(target)
                self.assertTrue(launcher.agent_profile_open())

    def test_stale_link_requires_advisory_lock_evidence(self):
        with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'PROFILE', Path(td)), patch.object(launcher.os, 'kill', side_effect=ProcessLookupError):
            (Path(td)/'lock').symlink_to('127.0.1.1:+987654')
            self.assertTrue(launcher.agent_profile_open())

    def test_profile_process_and_inspection_errors_block(self):
        for options in [dict(return_value=True), dict(side_effect=PermissionError())]:
            with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'PROFILE', Path(td)), patch.object(launcher, 'profile_process_open', **options):
                (Path(td)/'.parentlock').touch()
                self.assertTrue(launcher.agent_profile_open())

    def test_lock_changed_during_scan_blocks(self):
        with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'PROFILE', Path(td)), patch.object(launcher, 'profile_process_open', return_value=False), patch.object(launcher, 'profile_lock_snapshot', side_effect=[None, (1, 2, 3, '127.0.0.1:+1234')]):
            (Path(td)/'.parentlock').touch()
            self.assertTrue(launcher.agent_profile_open())

    def test_real_advisory_lock_held_by_another_process_blocks(self):
        with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'PROFILE', Path(td)):
            lockpath = Path(td)/'.parentlock'
            lockpath.touch()
            process = subprocess.Popen(['python3', '-c', 'import fcntl,sys; f=open(sys.argv[1],"r+b"); fcntl.lockf(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.read()', str(lockpath)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                self.assertTrue(launcher.agent_profile_open())
            finally:
                process.communicate('', timeout=5)

    def test_process_scan_distinguishes_exact_profile_from_personal_firefox(self):
        with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'PROFILE', Path(td)/'isolated'):
            proc = Path(td)/'proc'; folder = proc/'123'; folder.mkdir(parents=True)
            (folder/'cmdline').write_bytes(b'/usr/bin/firefox\0-profile\0/personal\0')
            self.assertFalse(launcher.profile_process_open(proc))
            (folder/'cmdline').write_bytes(('/usr/bin/firefox\0--profile='+str(launcher.PROFILE)+'\0').encode())
            self.assertTrue(launcher.profile_process_open(proc))

    def test_shutdown_lockfile_presence_alone_does_not_block_future_headless_tasks(self):
        with tempfile.TemporaryDirectory() as td, patch.object(launcher,'PROFILE',Path(td)):
            (Path(td)/'.parentlock').touch()
            self.assertFalse(launcher.agent_profile_open())
            (Path(td)/'lock').symlink_to('stale-profile-lock')
            self.assertTrue(launcher.agent_profile_open())

    def test_control_shortcut_remains_explicitly_opt_in(self):
        with patch.object(launcher, 'unit_state', return_value=('inactive', 0)):
            self.assertEqual(launcher.execute('gui', ['https://example.test/']), ['/usr/bin/firefox', '--marionette', '--no-remote', '-profile', str(launcher.PROFILE), 'https://example.test/'])


    def test_desktop_launch_does_not_touch_busy_agent_browser(self):
        with patch.object(launcher, 'unit_state', side_effect=AssertionError('desktop must be independent')), patch.object(launcher, 'socket_states', side_effect=AssertionError('must not inspect agent endpoint')):
            self.assertEqual(launcher.execute('desktop', ['https://example.test/']), ['/usr/bin/firefox', 'https://example.test/'])

    def test_agent_gui_cannot_override_profile_with_extra_arguments(self):
        for args in [['-P','daily'],['--profile=/daily'],['-profile','/daily']]:
            with self.subTest(args=args), patch.object(launcher,'stop_headless') as stop:
                with self.assertRaisesRegex(launcher.Busy,'profile overrides'):launcher.execute('gui',args)
                stop.assert_not_called()

    def test_listener_owner_requires_isolated_profile_not_daily_profile(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'net').mkdir();(root/'net/tcp').write_text('header\n0: 0100007F:0B0D 00000000:0000 0A 0:0 0:0 0 1000 0 42\n')
            folder=root/'123';folder.mkdir();(folder/'fd').mkdir();(folder/'fd/4').symlink_to('socket:[42]')
            (folder/'cmdline').write_bytes(b'firefox\0--marionette\0-profile\0'+str(launcher.PROFILE).encode()+b'\0')
            self.assertTrue(launcher.endpoint_isolated(root))
            (folder/'cmdline').write_bytes(b'firefox\0--marionette\0-profile\0/daily\0')
            self.assertFalse(launcher.endpoint_isolated(root))

    def test_unknown_endpoint_is_never_reused(self):
        with patch.object(launcher, 'socket_states', return_value=['0A']), patch.object(launcher, 'endpoint_isolated', return_value=False), patch.object(launcher.subprocess, 'run') as run:
            with self.assertRaisesRegex(launcher.Busy, 'unverified browser'):launcher.execute('start', [])
            run.assert_not_called()

    def test_missing_profile_never_falls_back_to_daily(self):
        self.profile_guard.stop()
        with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'PROFILE', Path(td)), patch.object(launcher.subprocess, 'run') as run:
            with self.assertRaisesRegex(launcher.Busy, 'never fall back'):
                launcher.execute('start', [])
            run.assert_not_called()


    def test_manual_login_uses_same_profile_without_automation_or_shutdown(self):
        with patch.object(launcher, 'unit_state', return_value=('inactive', 0)), patch.object(launcher, 'stop_headless') as stop:
            args = launcher.execute('login', ['https://accounts.example.test/'])
            self.assertEqual(args, ['/usr/bin/firefox', '--no-remote', '-profile', str(launcher.PROFILE), 'https://accounts.example.test/'])
            self.assertNotIn('--marionette', args);self.assertNotIn('--headless', args)
            launcher.prepare_mode.assert_called_once_with(False);stop.assert_not_called()

    def test_manual_login_never_stops_a_service_or_accepts_automation_flags(self):
        with patch.object(launcher, 'unit_state', return_value=('active', 123)), patch.object(launcher.subprocess, 'run') as run:
            with self.assertRaisesRegex(launcher.Busy, 'nothing was stopped'):launcher.execute('login', [])
            run.assert_not_called();launcher.prepare_mode.assert_not_called()
        for args in [['--marionette'], ['--profile=/daily'], ['--headless']]:
            with self.subTest(args=args), self.assertRaisesRegex(launcher.Busy, 'URLs only'):launcher.execute('login', args)

    def test_manual_environment_disables_real_startup_controls_without_mutating_caller(self):
        env={'MOZ_MARIONETTE':'1','MOZ_HEADLESS':'1','MOZ_REMOTE_ALLOW_SYSTEM_ACCESS':'1','DISPLAY':':0','UNCHANGED':'value'}
        manual=launcher.launch_environment('login',env)
        self.assertEqual(manual,{'DISPLAY':':0','UNCHANGED':'value'})
        self.assertEqual(launcher.launch_environment('gui',env),env)
        self.assertIn('MOZ_MARIONETTE',env)

    def test_modes_retain_profile_session_files_and_security_preferences(self):
        self.mode_guard.stop()
        with tempfile.TemporaryDirectory() as td, patch.object(launcher,'PROFILE',Path(td)), patch.object(launcher,'agent_profile_open',return_value=False), patch.object(launcher,'socket_states',return_value=[]):
            root=Path(td);prefs=root/'user.js'
            prefs.write_text('user_pref("marionette.enabled", true);\nuser_pref("security.tls.version.min", 3);\nuser_pref("browser.startup.page", 0);\n')
            (root/'sessionstore.jsonlz4').write_bytes(b'untouched-session-fixture');(root/'cookies.sqlite').write_bytes(b'untouched-cookie-fixture')
            launcher.prepare_mode(False)
            text=prefs.read_text();self.assertIn('"marionette.enabled", false',text);self.assertIn('"browser.startup.page", 3',text)
            self.assertIn('"browser.sessionstore.resume_session_once", true',text);self.assertIn('"security.tls.version.min", 3',text)
            self.assertEqual(text.count('"marionette.enabled"'),1);self.assertNotIn('dom.webdriver',text)
            self.assertEqual(prefs.stat().st_mode&0o777,0o600)
            launcher.prepare_mode(True);self.assertIn('"marionette.enabled", true',prefs.read_text())
            self.assertEqual((root/'sessionstore.jsonlz4').read_bytes(),b'untouched-session-fixture')
            self.assertEqual((root/'cookies.sqlite').read_bytes(),b'untouched-cookie-fixture')

    def test_mode_change_refuses_live_profile_and_late_open_without_writes(self):
        self.mode_guard.stop()
        for opened in [[True],[False,True]]:
            with tempfile.TemporaryDirectory() as td, patch.object(launcher,'PROFILE',Path(td)), patch.object(launcher,'agent_profile_open',side_effect=opened), patch.object(launcher,'socket_states',return_value=[]):
                p=Path(td)/'user.js';p.write_text('existing prefs')
                with self.assertRaises(launcher.Busy):launcher.prepare_mode(False)
                self.assertEqual(p.read_text(),'existing prefs');self.assertFalse((Path(td)/'user.js.before-launch-mode').exists())

    def test_automation_modes_prepare_real_automation_only_on_fresh_launch(self):
        with patch.object(launcher, 'unit_state', return_value=('inactive', 0)), patch.object(launcher,'agent_profile_open',return_value=False), patch.object(launcher,'socket_states',return_value=[]), patch.object(launcher.subprocess,'run'):
            launcher.execute('start',[]);launcher.prepare_mode.assert_called_once_with(True)
        launcher.prepare_mode.reset_mock()
        with patch.object(launcher,'stop_headless'):
            launcher.execute('gui',[]);launcher.prepare_mode.assert_called_once_with(True)

if __name__ == '__main__':unittest.main()
