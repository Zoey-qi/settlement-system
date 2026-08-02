' 静默启动脚本 - 不显示控制台窗口
' 放在 Windows 启动文件夹中即可开机自启

Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 获取脚本所在目录
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Python 路径
pythonExe = "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

' 启动 server.py（隐藏窗口）
WshShell.Run """" & pythonExe & """ """ & scriptDir & "\server.py""", 0, False

' 等待2秒后打开浏览器
WScript.Sleep 2000
WshShell.Run "http://localhost:5000"
