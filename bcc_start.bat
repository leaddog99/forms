@echo off
setlocal

set "VENV=C:\Users\john\PyCharm\venv"
set "PROJECT=C:\Users\john\PycharmProjects\forms"

cd /d "%PROJECT%"
call "%VENV%\Scripts\activate.bat"

echo.
echo Open: http://localhost:8009/forms/recipe_form_styled.html
echo.

uvicorn save_recipe_api:app --host 127.0.0.1 --port 8009 --reload --log-config log_config.json

endlocalcl