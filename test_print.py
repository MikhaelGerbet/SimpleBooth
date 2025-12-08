#!/usr/bin/env python3
"""Script de test pour l'impression"""
import requests
import sys

photo = sys.argv[1] if len(sys.argv) > 1 else "effect_20251202_225355.jpg"
r = requests.post('http://localhost:5000/print_photo', json={'photo_path': photo})
print(r.text)
