@echo off
echo Installing requirements...
python -m pip install -r requirements.txt

echo Installing PyInstaller...
python -m pip install pyinstaller

echo Building Roblox Auto-Rejoiner Executable...
echo This will take a moment...

python -m PyInstaller --noconfirm --onefile --noconsole --add-data "README.md;." "main_gui.py"

echo Build complete! The executable is dist\main_gui.exe.
pause
