@echo off
rem Abre o painel do TRNG. Deixe este arquivo na mesma pasta do trng_lava.py e do painel_trng.py.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw "%~dp0painel_trng.py" & exit /b)
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0painel_trng.py" & exit /b)
where python >nul 2>nul || (echo Python nao encontrado. Instale em https://www.python.org e marque "Add python.exe to PATH". & pause & exit /b)
python "%~dp0painel_trng.py"
if errorlevel 1 pause
