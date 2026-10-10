' Launch GLM dashboard fully hidden (no console window flash).
' When invoked via explorer or Task Scheduler, pythonw is NOT attached
' to any terminal/agent process tree: the launcher exiting does not
' kill the dashboard. Double-clicking this file also works.
Set sh = CreateObject("Wscript.Shell")
sh.CurrentDirectory = "E:\GIT_Space\glm-plan-dashboard"
sh.Run """E:\Program Files\anaconda3\pythonw.exe"" ""E:\GIT_Space\glm-plan-dashboard\main.py""", 0, False
