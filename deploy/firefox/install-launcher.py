#!/usr/bin/env python3
"""Install the guarded Firefox shortcut without changing profiles or browsers."""
from datetime import datetime
from pathlib import Path
import re
import shutil


def desktop_entry(source, helper):
    def replace(match):
        command = match.group(1)
        binary = next((p for p in ('/usr/lib/firefox/firefox', '/usr/bin/firefox', 'firefox')
                       if command == p or command.startswith(p+' ')), None)
        if binary is None:
            raise ValueError('Unexpected Firefox desktop command; inspect before overriding: '+command)
        path = str(helper).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
        return 'Exec="'+path+'" desktop'+command[len(binary):]
    result, count = re.subn(r'^Exec=(.*)$', replace, source, flags=re.MULTILINE)
    if not count:
        raise ValueError('No Firefox launch commands found')
    return result


def main():
    home=Path.home();here=Path(__file__).resolve().parent
    helper=home/'.local/bin/firefox-agent-browser';desktop=home/'.local/share/applications/firefox.desktop'
    generated=desktop_entry(Path('/usr/share/applications/firefox.desktop').read_text(), helper)
    planned=[(helper,(here/'firefox-agent-browser').read_bytes(),0o755),(desktop,generated.encode(),0o644),
             (home/'.config/systemd/user/firefox-agent-headless.service',(here/'firefox-agent-headless.service').read_bytes(),0o644),
             (home/'.local/share/applications/firefox-agent-control.desktop',(here/'firefox-agent-control.desktop').read_text().replace('/home/user', str(home)).encode(),0o644),
             (home/'.local/share/applications/firefox-agent-login.desktop',(here/'firefox-agent-login.desktop').read_text().replace('/home/user', str(home)).encode(),0o644)]
    backup=home/'.local/state/agent-foreman'/('firefox-launcher-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    for destination,data,mode in planned:
        if destination.exists() and destination.read_bytes()==data:
            continue
        backup.mkdir(parents=True,mode=0o700,exist_ok=True)
        if destination.exists():
            shutil.copy2(destination,backup/destination.name)
        else:
            (backup/(destination.name+'.absent')).touch()
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_name(destination.name+'.installing')
        temporary.write_bytes(data);temporary.chmod(mode);temporary.replace(destination)
        print('Installed',destination)
    if backup.exists():
        print('Previous launchers backed up to',backup)
    print('No Firefox process, profile settings or default-browser choice changed.')


if __name__=='__main__':main()
