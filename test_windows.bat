@echo off
cd /d %~dp0
call .venv\Scripts\activate.bat
python main.py self-test
pytest -q
pause
