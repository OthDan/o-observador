"""Gera o executável de O Observador.

Uso:
    python build.py                 # executável com janela de console (mostra os avisos)
    python build.py --sem-console   # executável silencioso, sem janela preta

O PyInstaller cria o executável para o sistema em que roda: para ter um .exe,
rode este script no Windows; para macOS ou Linux, rode no respectivo sistema.
"""
import os
import shutil
import subprocess
import venv
import sys
from pathlib import Path

AQUI = Path(__file__).parent.resolve()
NOME = "O-Observador"


def rodar(*cmd):
    print("> " + " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], cwd=AQUI, check=True)


def main():
    sem_console = "--sem-console" in sys.argv

    print("1/4  Preparando ambiente de build isolado (.venv-build)")
    amb = AQUI / ".venv-build"
    if not amb.exists():
        venv.create(amb, with_pip=True)
    py = amb / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    rodar(py, "-m", "pip", "install", "-q", "-r", "requirements.txt", "pyinstaller")
    if sys.platform == "win32":
        rodar(py, "-m", "pip", "install", "-q", "plyer")

    print("2/4  Empacotando")
    cmd = [py, "-m", "PyInstaller", "app.py", "--onefile", "--clean", "--noconfirm",
           "--name", NOME,
           "--collect-submodules", "uvicorn",
           "--collect-data", "certifi",
           "--hidden-import", "yaml",
           "--add-data", f"catalogo.yaml{os.pathsep}."]
    if sys.platform == "win32":
        cmd += ["--hidden-import", "plyer.platforms.win.notification"]
    if sem_console:
        cmd.append("--noconsole")
    rodar(*cmd)

    print("3/4  Copiando a configuração para ao lado do executável")
    shutil.copy(AQUI / "config.yaml", AQUI / "dist" / "config.yaml")

    print("4/4  Limpando arquivos temporários")
    shutil.rmtree(AQUI / "build", ignore_errors=True)
    shutil.rmtree(amb, ignore_errors=True)
    (AQUI / f"{NOME}.spec").unlink(missing_ok=True)

    exe = AQUI / "dist" / (NOME + (".exe" if sys.platform == "win32" else ""))
    print(f"\nPronto: {exe}")
    print("Edite dist/config.yaml para trocar os serviços; o banco será criado na mesma pasta.")


if __name__ == "__main__":
    main()
