"""Exact original real-console bootstrap selector."""
import os,pwd,subprocess
from pathlib import Path
from fixture_common import need

def aqua_command(command,environment):
    uid=Path("/dev/console").stat().st_uid
    need(uid==os.geteuid() and uid>0,"runner is not the real logged-in non-root console user")
    manager=subprocess.run(["/bin/launchctl","managername"],check=True,capture_output=True,text=True,timeout=5).stdout.strip()
    if manager=="Aqua":return command
    # The console user's own bootstrap is used. No imaginary GUI/display or
    # broad permissions change, and no credential env is forwarded.
    subprocess.run(["/usr/bin/sudo","-n","true"],check=True,timeout=5)
    user=pwd.getpwuid(uid).pw_name
    return ["/usr/bin/sudo","-n","/bin/launchctl","asuser",str(uid),
            "/usr/bin/sudo","-n","-u",user,"/usr/bin/env","-i",
            *[key+"="+value for key,value in environment.items()],*command]

