<#
Instala o repara todas las dependencias de la aplicación y la inicia.
Ejecutar desde PowerShell, dentro de la carpeta del proyecto:
  powershell -ExecutionPolicy Bypass -File .\instalar_y_ejecutar.ps1
#>

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$requirements = Join-Path $projectRoot "requirements.txt"
$appFile = Join-Path $projectRoot "web_app_laser-v4.py"

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creando entorno virtual .venv..." -ForegroundColor Cyan
    python -m venv (Join-Path $projectRoot ".venv")
}

Write-Host "Reparando dependencias. Esto puede tardar unos minutos..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install --upgrade --force-reinstall --no-cache-dir -r $requirements

Write-Host "Comprobando módulos..." -ForegroundColor Cyan
& $venvPython -c "import cv2, ezdxf, matplotlib, pandas, pdfplumber, shapely, streamlit, svgpathtools; from dateutil import parser; print('Dependencias verificadas correctamente.')"

Write-Host "Iniciando Cotizador Láser PRO..." -ForegroundColor Green
& $venvPython -m streamlit run $appFile
