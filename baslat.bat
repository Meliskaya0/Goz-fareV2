@echo off
cd /d "%~dp0"
for %%f in (*fare.py) do (
    "C:\Users\Melis\AppData\Local\Programs\Python\Python314\python.exe" -u "%%f"
)
pause
