Set ws = CreateObject("WScript.Shell")
Set sc = ws.CreateShortcut("C:\Users\Administrator\Desktop\帕基尔结算系统_公网链接.lnk")
sc.TargetPath = "C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system\启动公网链接.bat"
sc.WorkingDirectory = "C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system"
sc.IconLocation = "C:\Windows\System32\shell32.dll,13"
sc.Description = "启动帕基尔结算系统并生成公网链接"
sc.Save
