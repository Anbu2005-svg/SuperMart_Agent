@echo off
cd /d "%~dp0"
echo ========================================================
echo   Running Barcode Benchmark & Validation Suites...
echo ========================================================
.\.venv\Scripts\python.exe scripts\run_production_grade_validation.py
echo.
.\.venv\Scripts\python.exe scripts\download_and_stress_test_barcodes.py
pause
