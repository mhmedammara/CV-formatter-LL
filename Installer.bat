@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Installation du CV Formatter Logiclever...
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo.
    echo Echec de l'installation : verifiez que Python 3.11+ est installe ^(https://www.python.org^).
    pause
    exit /b 1
)
python -c "from cv_formatter import fonts; fonts.install_fonts(); print('Polices Lexend : OK')"
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Un fichier .env a ete cree : ouvrez-le et collez votre cle OpenAI apres OPENAI_API_KEY=
    notepad ".env"
)
echo.
echo Installation terminee.
pause
