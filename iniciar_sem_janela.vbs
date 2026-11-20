' Inicia o Stream Deck em segundo plano (sem janela de console).
' Para abrir junto com o Windows: Win+R, digite shell:startup e
' coloque um ATALHO deste arquivo nessa pasta.
' Para ver o PIN e o endereco, abra o config.json (PIN) e use o IP do PC.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "cmd /c where pyw >nul 2>nul && (pyw -3 server.py) || (pythonw server.py)", 0, False
