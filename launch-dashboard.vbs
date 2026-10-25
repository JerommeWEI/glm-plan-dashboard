' Launch GLM dashboard fully hidden (no console window flash).
' When invoked via explorer or Task Scheduler, pythonw is NOT attached
' to any terminal/agent process tree: the launcher exiting does not
' kill the dashboard. Double-clicking this file also works.
' Keep this file pure ASCII: wscript reads .vbs as ANSI, so non-ASCII
' paths written as UTF-8 turn to mojibake. Resolve paths at runtime
' instead (script dir via ScriptFullName; pythonw via PATH).
Set sh = CreateObject("Wscript.Shell")
sh.CurrentDirectory = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run "pythonw main.py", 0, False
