#!/usr/bin/env python3
"""
storage_tier_gui.py
===================

Janela gráfica (tkinter) — SOMENTE LEITURA — para visualizar o Access Tier de
Storage Accounts do Azure, o tamanho usado e o número de blobs, e ver para
quais tiers cada conta poderia mudar.

Fluxo:
  1. Escolha a subscription no topo (há um campo de filtro, pois o tenant pode
     ter centenas).
  2. Clique em "Carregar storages".
  3. A tabela mostra: Nome, Tier atual, Tamanho usado, Nº de blobs, Containers, RG.
  4. Clique em uma linha para ver, no painel inferior, o tier atual e as opções
     de mudança (Cool / Cold / Archive) com suas observações.

Não altera nada e não acessa o conteúdo (plano de dados) dos blobs — usa apenas
APIs de gestão (ARM) e métricas do Azure Monitor, que precisam só de 'Reader'.

Requisitos: az login feito no tenant desejado; pip install -r requirements.txt
Execução:   python storage_tier_gui.py   (abre a janela)
"""

from __future__ import annotations

import argparse
import queue
import threading
import traceback
from typing import Optional

import tkinter as tk
from tkinter import ttk, messagebox

# Reaproveita toda a lógica já testada do script de console.
from storage_tier_manager import (
    Account,
    Metrics,
    apply_blob_tier,
    build_credential,
    collect_blobs,
    fetch_all_metrics,
    human_bytes,
    human_count,
    list_accounts,
    make_blob_service,
    possible_targets,
    TIER_NOTAS,
)
from azure.mgmt.subscription import SubscriptionClient
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.storage.models import StorageAccountUpdateParameters
from azure.mgmt.monitor import MonitorManagementClient


APP_TITLE = "Azure Storage — Access Tier (somente leitura)"


class StorageTierGUI(tk.Tk):
    def __init__(self, tenant_id: Optional[str] = None):
        super().__init__()
        self.tenant_id = tenant_id
        self.title(APP_TITLE)
        self.geometry("980x640")
        self.minsize(820, 520)

        # Estado
        self.credential = build_credential(tenant_id)
        self.subs: list[tuple[str, str, str]] = []   # (display, sub_id, state)
        self.accounts: list[Account] = []
        self.metrics: dict[str, Metrics] = {}
        self.current_sub_id: Optional[str] = None
        self.sort_state: dict[str, bool] = {}        # coluna -> reverso?
        self.ui_queue: "queue.Queue" = queue.Queue()  # resultados de threads -> UI

        self._build_widgets()
        self._drain_queue()                # inicia o "pump" da fila na thread da UI
        self._load_subscriptions_async()

    # ------------------------------------------------------------------ UI
    def _build_widgets(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")  # tema nativo no Windows
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24)
        style.configure("Banner.TLabel", foreground="#7a3b00")

        # ---- Barra superior: filtro + subscription + botão
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Filtro:").grid(row=0, column=0, sticky="w")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_sub_filter())
        self.filter_entry = ttk.Entry(top, textvariable=self.filter_var, width=22)
        self.filter_entry.grid(row=0, column=1, padx=(4, 12), sticky="w")

        ttk.Label(top, text="Subscription:").grid(row=0, column=2, sticky="w")
        self.sub_var = tk.StringVar()
        self.sub_combo = ttk.Combobox(top, textvariable=self.sub_var, width=52,
                                      state="readonly")
        self.sub_combo.grid(row=0, column=3, padx=(4, 12), sticky="we")

        self.load_btn = ttk.Button(top, text="Carregar storages",
                                   command=self._load_accounts_async)
        self.load_btn.grid(row=0, column=4, sticky="e")
        top.columnconfigure(3, weight=1)

        # ---- Banner somente leitura
        banner = ttk.Label(
            self,
            text="Somente leitura por padrão. Clique com o botão direito numa conta "
                 "para mudar o tier (há opção de simular).",
            style="Banner.TLabel", padding=(10, 0),
        )
        banner.pack(side=tk.TOP, fill=tk.X)

        # ---- Tabela (Treeview)
        mid = ttk.Frame(self, padding=(10, 6))
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        columns = ("nome", "tier", "tamanho", "blobs", "containers", "rg")
        headers = {
            "nome": ("Nome", 270, "w"),
            "tier": ("Tier", 70, "center"),
            "tamanho": ("Tamanho usado", 120, "e"),
            "blobs": ("Blobs", 90, "e"),
            "containers": ("Containers", 90, "e"),
            "rg": ("Resource group", 210, "w"),
        }
        self.tree = ttk.Treeview(mid, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            text, width, anchor = headers[col]
            self.tree.heading(col, text=text, command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor=anchor, stretch=(col in ("nome", "rg")))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._on_right_click)        # menu de contexto
        self.tree.bind("<Button-2>", self._on_right_click)        # mouse do mac

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)

        # ---- Painel de detalhe
        det = ttk.LabelFrame(self, text="Detalhe da conta selecionada", padding=(10, 6))
        det.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 6))
        self.detail = tk.Text(det, height=9, wrap="word", state="disabled",
                              background="#f7f7f7", relief="flat")
        self.detail.pack(fill=tk.X)

        # ---- Barra de status
        self.status_var = tk.StringVar(value="Carregando subscriptions...")
        status = ttk.Label(self, textvariable=self.status_var, relief="sunken",
                           anchor="w", padding=(8, 3))
        status.pack(side=tk.BOTTOM, fill=tk.X)

    # ------------------------------------------------------------- helpers
    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _run_async(self, work, on_done, on_error=None) -> None:
        """Executa `work()` numa thread e entrega o resultado na thread da UI.

        A entrega é feita via fila (thread-safe); a thread da UI drena a fila em
        _drain_queue(). Nunca chamamos métodos tkinter direto de outra thread.
        """
        def runner():
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                self.ui_queue.put(lambda: (on_error or self._default_error)(exc, tb))
                return
            self.ui_queue.put(lambda: on_done(result))
        threading.Thread(target=runner, daemon=True).start()

    def _drain_queue(self) -> None:
        """Executa, na thread da UI, os callbacks enfileirados pelas threads."""
        try:
            while True:
                cb = self.ui_queue.get_nowait()
                try:
                    cb()
                except Exception as exc:  # noqa: BLE001
                    self._default_error(exc, traceback.format_exc())
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

    def _default_error(self, exc: Exception, tb: str) -> None:
        self._set_status(f"Erro: {str(exc).splitlines()[0][:120]}")

    # --------------------------------------------------------- subscriptions
    def _load_subscriptions_async(self) -> None:
        self._set_status("Carregando subscriptions...")

        def work():
            client = SubscriptionClient(self.credential)
            out = []
            for s in client.subscriptions.list():
                out.append((s.display_name or "(sem nome)", s.subscription_id,
                            str(s.state)))
            out.sort(key=lambda x: x[0].lower())
            return out

        def done(subs):
            self.subs = subs
            self._apply_sub_filter()
            self._set_status(f"{len(subs)} subscriptions. Escolha uma e clique em "
                             f"'Carregar storages'.")

        self._run_async(work, done)

    def _apply_sub_filter(self) -> None:
        termo = self.filter_var.get().strip().lower()
        visiveis = [s for s in self.subs if not termo or termo in s[0].lower()]
        valores = [f"{disp}   [{sid}]" for disp, sid, _ in visiveis]
        self._visiveis = visiveis
        self.sub_combo["values"] = valores
        if valores:
            # Mantém seleção se ainda visível; senão seleciona a primeira.
            if self.sub_var.get() not in valores:
                self.sub_combo.current(0)
        else:
            self.sub_var.set("")

    def _selected_sub_id(self) -> Optional[str]:
        idx = self.sub_combo.current()
        if idx < 0 or idx >= len(getattr(self, "_visiveis", [])):
            return None
        return self._visiveis[idx][1]

    # --------------------------------------------------------------- accounts
    def _load_accounts_async(self) -> None:
        sub_id = self._selected_sub_id()
        if not sub_id:
            self._set_status("Selecione uma subscription primeiro.")
            return
        self.load_btn.config(state="disabled")
        self.current_sub_id = sub_id
        self._clear_table()
        self._set_status("Listando storage accounts...")

        def work():
            storage = StorageManagementClient(self.credential, sub_id)
            return list_accounts(storage, sub_id)

        def done(accounts):
            self.accounts = accounts
            self.metrics = {}
            self._populate_table(accounts, with_metrics=False)
            if not accounts:
                self.load_btn.config(state="normal")
                self._set_status("Nenhuma storage account nesta subscription.")
                return
            self._set_status(f"{len(accounts)} conta(s). Coletando tamanho/blobs "
                             f"via Azure Monitor...")
            self._load_metrics_async(sub_id, accounts)

        self._run_async(work, done, on_error=self._accounts_error)

    def _accounts_error(self, exc: Exception, tb: str) -> None:
        self.load_btn.config(state="normal")
        self._set_status(f"Erro ao listar contas: {str(exc).splitlines()[0][:120]}")

    def _load_metrics_async(self, sub_id: str, accounts: list[Account]) -> None:
        def work():
            monitor = MonitorManagementClient(self.credential, sub_id)
            return fetch_all_metrics(monitor, accounts)

        def done(metrics_map):
            self.metrics = metrics_map
            self._populate_table(self.accounts, with_metrics=True)
            total = sum((m.used_bytes or 0) for m in metrics_map.values())
            self.load_btn.config(state="normal")
            self._set_status(f"Pronto. {len(accounts)} conta(s) — total usado: "
                             f"{human_bytes(total)}. Clique numa linha para detalhes.")

        def err(exc, tb):
            self.load_btn.config(state="normal")
            self._set_status("Contas listadas; métricas indisponíveis "
                             f"({str(exc).splitlines()[0][:80]}).")

        self._run_async(work, done, on_error=err)

    # ------------------------------------------------------------------ table
    def _clear_table(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def _populate_table(self, accounts: list[Account], with_metrics: bool) -> None:
        self._clear_table()
        for a in accounts:
            m = self.metrics.get(a.name) if with_metrics else None
            if with_metrics:
                size = human_bytes(m.used_bytes) if m else "n/d"
                blobs = human_count(m.blob_count) if m else "n/d"
                conts = human_count(m.container_count) if m else "n/d"
            else:
                size = blobs = conts = "..."
            self.tree.insert(
                "", tk.END, iid=a.name,
                values=(a.name, a.access_tier or "-", size, blobs, conts, a.resource_group),
            )

    def _sort_by(self, col: str) -> None:
        if not self.accounts:
            return
        reverse = self.sort_state.get(col, False)
        self.sort_state[col] = not reverse

        def metric_val(a: Account, attr: str):
            m = self.metrics.get(a.name)
            return getattr(m, attr) if m and getattr(m, attr) is not None else -1

        keymap = {
            "nome": lambda a: a.name.lower(),
            "tier": lambda a: (a.access_tier or ""),
            "rg": lambda a: a.resource_group.lower(),
            "tamanho": lambda a: metric_val(a, "used_bytes"),
            "blobs": lambda a: metric_val(a, "blob_count"),
            "containers": lambda a: metric_val(a, "container_count"),
        }
        self.accounts.sort(key=keymap.get(col, keymap["nome"]), reverse=reverse)
        self._populate_table(self.accounts, with_metrics=bool(self.metrics))

    # ----------------------------------------------------------------- detail
    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        name = sel[0]
        account = next((a for a in self.accounts if a.name == name), None)
        if account is None:
            return
        self._render_detail(account, self.metrics.get(name))

    def _render_detail(self, account: Account, metrics: Optional[Metrics]) -> None:
        lines = []
        lines.append(f"{account.name}")
        lines.append(f"    Resource group : {account.resource_group}")
        lines.append(f"    Local          : {account.location}")
        lines.append(f"    Kind / SKU     : {account.kind} / {account.sku}")
        lines.append(f"    Tier ATUAL     : {account.access_tier or '(não suporta access tier)'}")
        if metrics is not None:
            lines.append(f"    Tamanho usado  : {human_bytes(metrics.used_bytes)}"
                         f"   (em blobs: {human_bytes(metrics.blob_bytes)})")
            lines.append(f"    Nº de blobs    : {human_count(metrics.blob_count)}"
                         f"   (containers: {human_count(metrics.container_count)})")
        if account.access_tier is None:
            lines.append("    Pode mudar para: — (este tipo de conta não tem access tier)")
        else:
            lines.append("    Pode mudar para:")
            for t in possible_targets(account, include_archive=True):
                marca = " [somente blobs]" if t == "Archive" else ""
                lines.append(f"        • {t}{marca} — {TIER_NOTAS.get(t, '')}")

        self.detail.config(state="normal")
        self.detail.delete("1.0", tk.END)
        self.detail.insert(tk.END, "\n".join(lines))
        self.detail.config(state="disabled")

    # ------------------------------------------------- menu de contexto / mudar
    def _on_right_click(self, event) -> None:
        """Abre o menu pop-up sobre a linha clicada para mudar o tier."""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        account = next((a for a in self.accounts if a.name == iid), None)
        if account is None:
            return

        menu = tk.Menu(self, tearoff=0)
        submenu = tk.Menu(menu, tearoff=0)
        current = account.access_tier
        for t in ("Hot", "Cool", "Cold", "Archive"):
            # Desabilita o tier atual da conta (nada a mudar lá).
            state = "disabled" if t == current else "normal"
            rotulo = t + (" [somente blobs]" if t == "Archive" else "")
            submenu.add_command(
                label=rotulo, state=state,
                command=lambda tt=t, a=account: self._prompt_change(a, tt),
            )
        menu.add_cascade(label="Mudar tier do blob para", menu=submenu)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _prompt_change(self, account: Account, target: str) -> None:
        """Diálogo de confirmação com opção de simular (dry-run, ligada por padrão)."""
        dlg = tk.Toplevel(self)
        dlg.title("Mudar tier do blob")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.grab_set()

        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill=tk.BOTH, expand=True)

        resumo = (
            f"Conta:        {account.name}\n"
            f"Resource grp: {account.resource_group}\n"
            f"Tier atual:   {account.access_tier or '(n/d)'}\n"
            f"Novo tier:    {target}"
        )
        ttk.Label(frm, text=resumo, justify="left", font=("Consolas", 9)).pack(anchor="w")

        aviso = (
            "Atenção: mudar de tier pode gerar custos de retirada/exclusão antecipada.\n"
            "Archive deixa os blobs offline (leitura exige reidratação).\n"
            "Requer permissão de escrita/dados — provavelmente retornará erro."
        )
        ttk.Label(frm, text=aviso, justify="left", foreground="#7a3b00",
                  wraplength=420).pack(anchor="w", pady=(8, 8))

        dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm, variable=dry,
            text="Apenas simular (dry-run) — não altera nada",
        ).pack(anchor="w")

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=(12, 0))

        def on_confirm():
            dry_run = dry.get()
            dlg.destroy()
            self._start_change(account, target, dry_run)

        ttk.Button(btns, text="Cancelar", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Confirmar", command=on_confirm).pack(
            side=tk.RIGHT, padx=(0, 8))

        dlg.bind("<Escape>", lambda *_: dlg.destroy())
        dlg.update_idletasks()
        # centraliza sobre a janela principal
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _start_change(self, account: Account, target: str, dry_run: bool) -> None:
        modo = "Simulando" if dry_run else "Tentando alterar"
        self._set_status(f"{modo} tier de '{account.name}' para {target}...")
        sub_id = self.current_sub_id

        def work():
            return self._change_tier(sub_id, account, target, dry_run)

        def done(result):
            ok, titulo, msg = result
            self._set_status(f"{titulo}: {account.name} -> {target}")
            (messagebox.showinfo if ok else messagebox.showwarning)(titulo, msg, parent=self)

        def err(exc, tb):
            primeira = str(exc).splitlines()[0]
            self._set_status(f"Erro ao mudar tier: {primeira[:100]}")
            messagebox.showerror(
                "Erro ao mudar tier",
                f"Não foi possível mudar o tier de '{account.name}' para {target}.\n\n"
                f"{primeira[:500]}",
                parent=self,
            )

        self._run_async(work, done, on_error=err)

    def _change_tier(self, sub_id, account: Account, target: str, dry_run: bool):
        """Executa (ou simula) a mudança de tier. Roda em thread de trabalho.

        Retorna (ok: bool, titulo: str, mensagem: str). Exceções sobem para on_error.
        """
        if dry_run:
            msg = (
                "SIMULAÇÃO — nada foi alterado.\n\n"
                f"Conta '{account.name}':\n"
                f"  • tier padrão da conta: {account.access_tier} -> {target}\n"
                f"  • blobs (block) seriam movidos para {target}\n\n"
                "Desmarque 'Apenas simular' para tentar de verdade."
            )
            return (True, "Simulação", msg)

        # Tentativa real. Para usuário só-leitura, falha já no 1º passo (sem
        # tocar em nenhum blob), que é o comportamento esperado.
        storage = StorageManagementClient(self.credential, sub_id)
        passos = []

        # 1) Tier padrão da conta (plano de gestão) — Archive não vale para a conta.
        if target != "Archive" and account.access_tier and account.access_tier != target:
            storage.storage_accounts.update(
                account.resource_group, account.name,
                StorageAccountUpdateParameters(access_tier=target),
            )
            passos.append(f"Tier padrão da conta alterado para {target}.")

        # 2) Tier dos blobs (plano de dados) — exige role de dados.
        svc = make_blob_service(account, self.credential, storage)
        items, already, skipped = collect_blobs(svc, target)
        failures = apply_blob_tier(svc, items, target, workers=8, dry_run=False)
        passos.append(
            f"Blobs: {len(items) - len(failures)} alterados, {len(failures)} falha(s), "
            f"{already} já em {target}."
        )
        return (True, "Concluído", "\n".join(passos))


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--tenant", help="Tenant ID (default: sessão atual do az).")
    args = parser.parse_args()

    app = StorageTierGUI(tenant_id=args.tenant)
    app.mainloop()


if __name__ == "__main__":
    main()
