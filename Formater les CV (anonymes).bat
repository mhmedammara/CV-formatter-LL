@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Version anonyme : initiales, sans photo ni coordonnees personnelles. Sortie dans le dossier « sortie anonyme ».
python -m cv_formatter %* --anonymiser --sortie "sortie anonyme" --ouvrir
echo.
pause
