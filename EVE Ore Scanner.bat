@echo off
title EVE Ore Scanner
cd /d "%~dp0"
pythonw ore_scanner.py 2>nul || python ore_scanner.py
