import sys
from pathlib import Path

# Permite ejecutar `pytest` desde la raíz del repo sin instalar el paquete.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
