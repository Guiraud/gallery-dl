#!/usr/bin/env python3
"""Interface grafique macOS pour lancer gallery-dl.

Ce module fournit une application Tkinter légère qui permet :
    - de coller une ou plusieurs URLs à télécharger,
    - de choisir un dossier de destination,
    - d'appeler `python -m gallery_dl` et d'afficher la sortie en direct.

L'app est pensée pour être empaquetée en .app (via PyInstaller par exemple),
mais fonctionne aussi en mode script (`python macos_app/gallerydl_gui.py`).
"""

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import List, Optional

try:  # pragma: no cover - handled both as script and module
    from . import twitter_index
except ImportError:  # pragma: no cover
    import twitter_index  # type: ignore


class GalleryDLApp(tk.Tk):
    """Fenêtre principale de l'interface gallery-dl."""

    def __init__(self) -> None:
        super().__init__()
        self.title("gallery-dl pour macOS")
        self.geometry("720x520")
        self.minsize(640, 480)

        self._directory_var = tk.StringVar(value=str(Path.cwd()))
        self._status_var = tk.StringVar(value="Prêt.")
        self._cookies_file_var = tk.StringVar()
        self._browser_var = tk.StringVar()
        self._browser_domain_var = tk.StringVar(value="x.com")
        self._build_html_var = tk.BooleanVar(value=True)
        self._last_urls: List[str] = []
        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._log_queue: queue.Queue = queue.Queue()
        self._poll_after_id: Optional[str] = None

        self._build_widgets()
        self._poll_queue()

    # region UI setup --------------------------------------------------
    def _build_widgets(self) -> None:
        """Crée les widgets Tkinter."""
        main = ttk.Frame(self, padding=15)
        main.pack(fill=tk.BOTH, expand=True)

        # URLs à télécharger
        ttk.Label(main, text="URLs (une par ligne) :").pack(anchor=tk.W)
        self._urls_input = ScrolledText(main, height=6, wrap=tk.WORD)
        self._urls_input.pack(fill=tk.X, pady=(0, 10))

        # Choix du dossier de destination
        dir_frame = ttk.Frame(main)
        dir_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(dir_frame, text="Dossier de sortie :").pack(anchor=tk.W)
        dir_entry = ttk.Entry(dir_frame, textvariable=self._directory_var)
        dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(
            dir_frame,
            text="Parcourir…",
            command=self._ask_directory,
        ).pack(side=tk.LEFT)

        # Cookies
        ttk.Label(main, text="Cookies (optionnel) :").pack(anchor=tk.W)
        cookies_frame = ttk.Frame(main)
        cookies_frame.pack(fill=tk.X, pady=(0, 8))

        cookies_entry = ttk.Entry(cookies_frame, textvariable=self._cookies_file_var)
        cookies_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(
            cookies_frame,
            text="Fichier…",
            command=self._ask_cookies_file,
        ).pack(side=tk.LEFT)

        browser_frame = ttk.Frame(main)
        browser_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(
            browser_frame, text="Ou utiliser les cookies du navigateur :"
        ).pack(anchor=tk.W, side=tk.LEFT)
        browser_combo = ttk.Combobox(
            browser_frame,
            textvariable=self._browser_var,
            values=[
                "",
                "safari",
                "firefox",
                "chrome",
                "edge",
                "brave",
                "vivaldi",
                "chromium",
                "arc",
            ],
            state="readonly",
            width=12,
        )
        browser_combo.pack(side=tk.LEFT, padx=(8, 8))
        ttk.Label(browser_frame, text="Domaine :").pack(side=tk.LEFT)
        ttk.Entry(browser_frame, textvariable=self._browser_domain_var, width=14).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        ttk.Checkbutton(
            main,
            text="Générer une page HTML X.com (nécessite --write-metadata)",
            variable=self._build_html_var,
        ).pack(anchor=tk.W, pady=(4, 10))

        # Boutons d'action
        button_frame = ttk.Frame(main)
        button_frame.pack(fill=tk.X, pady=(0, 10))

        self._start_button = ttk.Button(
            button_frame, text="Télécharger", command=self._start_download
        )
        self._start_button.pack(side=tk.LEFT)

        self._stop_button = ttk.Button(
            button_frame,
            text="Arrêter",
            command=self._stop_download,
            state=tk.DISABLED,
        )
        self._stop_button.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Button(
            button_frame,
            text="À propos",
            command=self._show_about,
        ).pack(side=tk.RIGHT)

        # Log de sortie de gallery-dl
        ttk.Label(main, text="Sortie :").pack(anchor=tk.W)
        self._output_text = ScrolledText(main, height=12, state=tk.DISABLED)
        self._output_text.pack(fill=tk.BOTH, expand=True)

        status_bar = ttk.Label(self, textvariable=self._status_var, anchor=tk.W)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=5)

    # endregion UI setup -----------------------------------------------

    def _ask_directory(self) -> None:
        directory = filedialog.askdirectory(initialdir=self._directory_var.get())
        if directory:
            self._directory_var.set(directory)

    def _show_about(self) -> None:
        messagebox.showinfo(
            title="À propos",
            message=(
                "Interface graphique pour gallery-dl\n\n"
                "• Nécessite Python 3.8+ et gallery-dl installé\n"
                "• Utilise le module standard Tkinter\n"
                "• Distribuée sous licence GPLv2 (comme gallery-dl)"
            ),
        )

    # region Command execution -----------------------------------------
    def _start_download(self) -> None:
        urls = self._collect_urls()
        if not urls:
            messagebox.showwarning("Avertissement", "Merci de saisir au moins une URL.")
            return

        try:
            output_dir = Path(self._directory_var.get()).expanduser()
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Erreur", f"Impossible d'utiliser ce dossier : {exc}")
            return

        command = [sys.executable, "-m", "gallery_dl", "-d", str(output_dir)]

        cookies_file = self._cookies_file_var.get().strip()
        if cookies_file:
            command.extend(["--cookies", str(Path(cookies_file).expanduser())])

        browser = self._browser_var.get().strip()
        domain = self._browser_domain_var.get().strip() or "x.com"
        if browser:
            command.extend(["--cookies-from-browser", f"{browser}/{domain}"])

        target_x = any("x.com" in url or "twitter.com" in url for url in urls)
        if target_x and self._build_html_var.get():
            command.append("--write-metadata")

        self._last_urls = urls
        command.extend(urls)
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        self._drain_queue()
        self._append_text(f"Commande lancée : {' '.join(command)}\n")

        self._reader_thread = threading.Thread(
            target=self._read_process_output, daemon=True
        )
        self._reader_thread.start()

        self._start_button.configure(state=tk.DISABLED)
        self._stop_button.configure(state=tk.NORMAL)
        self._status_var.set("Téléchargement en cours…")

    def _stop_download(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            self._status_var.set("Arrêt en cours…")
        else:
            self._status_var.set("Aucun téléchargement actif.")
        self._start_button.configure(state=tk.NORMAL)
        self._stop_button.configure(state=tk.DISABLED)

    def _collect_urls(self) -> List[str]:
        raw = self._urls_input.get("1.0", tk.END)
        urls = [line.strip() for line in raw.splitlines() if line.strip()]
        return urls

    def _read_process_output(self) -> None:
        """Thread secondaire qui lit la sortie du processus et l'alimente dans la file."""
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            self._log_queue.put(line)
        self._process.wait()
        self._log_queue.put(f"\nProcessus terminé (code {self._process.returncode}).\n")
        self._log_queue.put("__EOF__")

    def _poll_queue(self) -> None:
        """Transfère les messages de la file dans la zone de texte UI."""
        try:
            while True:
                item = self._log_queue.get_nowait()
                if item == "__EOF__":
                    self._start_button.configure(state=tk.NORMAL)
                    self._stop_button.configure(state=tk.DISABLED)
                    status = (
                        "Téléchargement terminé."
                        if self._process and self._process.returncode == 0
                        else "Commande terminée avec erreurs."
                    )
                    self._status_var.set(status)
                    if (
                        status == "Téléchargement terminé."
                        and self._build_html_var.get()
                        and any("x.com" in url or "twitter.com" in url for url in self._last_urls)
                    ):
                        self._generate_html_index()
                    self._process = None
                    return
                self._append_text(item)
        except queue.Empty:
            pass
        finally:
            # Planifie la prochaine vérification
            self._poll_after_id = self.after(150, self._poll_queue)

    def _append_text(self, text: str) -> None:
        self._output_text.configure(state=tk.NORMAL)
        self._output_text.insert(tk.END, text)
        self._output_text.see(tk.END)
        self._output_text.configure(state=tk.DISABLED)

    # endregion Command execution -------------------------------------

    def _generate_html_index(self) -> None:
        try:
            output_dir = Path(self._directory_var.get()).expanduser()
            created = twitter_index.build_indexes(output_dir, recursive=True, overwrite=True)
        except Exception as exc:  # pragma: no cover - UI surface
            self._append_text(f"Erreur génération HTML : {exc}\n")
            return

        if not created:
            self._append_text("Aucun index HTML généré (métadonnées manquantes ?).\n")
        else:
            for path in created:
                self._append_text(f"Index HTML créé : {path}\n")

    def _ask_cookies_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Sélectionner un fichier cookies",
            filetypes=[("cookies.txt", "*.txt"), ("Tous les fichiers", "*.*")],
        )
        if file_path:
            self._cookies_file_var.set(file_path)

    def _drain_queue(self) -> None:
        try:
            while True:
                self._log_queue.get_nowait()
        except queue.Empty:
            pass

    def destroy(self) -> None:  # type: ignore[override]
        if self._poll_after_id is not None:
            self.after_cancel(self._poll_after_id)
            self._poll_after_id = None
        if self._process and self._process.poll() is None:
            self._process.terminate()
        super().destroy()


def main() -> None:
    app = GalleryDLApp()
    app.mainloop()


if __name__ == "__main__":
    main()
