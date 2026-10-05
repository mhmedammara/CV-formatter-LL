@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Version anonyme des CV du dossier Input : initiales, sans photo ni coordonnees personnelles. Sortie dans « sortie anonyme ».
python -m cv_formatter %* --anonymiser --sortie "sortie anonyme" --ouvrir
echo.
pause
