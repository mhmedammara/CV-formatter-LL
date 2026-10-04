@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Traite le dossier drive-download-* le plus recent (ou les dossiers/fichiers glisses sur ce fichier).
python -m cv_formatter %* --ouvrir
echo.
pause
