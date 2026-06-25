#!/usr/bin/env python3
"""
storage_tier_tui.py
===================

Interface de terminal em ASCII para Azure Cloud Shell/SSH/headless.

Nao usa curses nem dependencias extras: apenas input/print, com telas em estilo
Clipper/Pascal para selecionar subscription, storage account e ver detalhes.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional, Sequence, TypeVar

from storage_tier_manager import (
    Account,
    ClientAuthenticationError,
    HttpResponseError,
    Metrics,
    MonitorManagementClient,
    StorageManagementClient,
    SubscriptionClient,
    apply_blob_tier,
    build_credential,
    collect_blobs,
    confirm,
    fetch_all_metrics,
    friendly_azure_error,
    human_bytes,
    human_count,
    list_accounts,
    make_blob_service,
    possible_targets,
    subscription_name_matches_filter,
    TIER_NOTAS,
    update_account_tier,
)


T = TypeVar("T")
WIDTH = 96
PAGE_SIZE = 14


def clear_screen() -> None:
    """Limpa a tela quando o terminal suporta ANSI; senao separa com linhas."""
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")
    else:
        print("\n" * 3)


def pause(message: str = "Pressione Enter para continuar...") -> None:
    try:
        input(message)
    except (EOFError, KeyboardInterrupt):
        print("\nSaindo.")
        raise SystemExit(130)


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSaindo.")
        raise SystemExit(130)


def fit(text: object, width: int) -> str:
    value = str(text if text is not None else "")
    value = " ".join(value.split())
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."


def box(title: str, lines: Sequence[str], width: int = WIDTH) -> None:
    title = f" {fit(title, width - 4)} "
    left = (width - 2 - len(title)) // 2
    right = width - 2 - len(title) - left
    print("+" + ("-" * left) + title + ("-" * right) + "+")
    for line in lines:
        print("| " + fit(line, width - 4).ljust(width - 4) + " |")
    print("+" + ("-" * (width - 2)) + "+")


def menu(
    title: str,
    items: Sequence[T],
    formatter: Callable[[T], str],
    matcher: Callable[[T, str], bool],
    empty_message: str,
) -> Optional[T]:
    filtro = ""
    page = 0
    while True:
        visible = [item for item in items if matcher(item, filtro)]
        pages = max(1, (len(visible) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages - 1)
        start = page * PAGE_SIZE
        current = visible[start : start + PAGE_SIZE]

        clear_screen()
        header = [
            f"Itens: {len(visible)} de {len(items)}"
            + (f" | filtro: {filtro}" if filtro else ""),
            f"Pagina: {page + 1}/{pages}",
            "",
        ]
        if current:
            rows = [f"{idx:>2}) {formatter(item)}" for idx, item in enumerate(current, 1)]
        else:
            rows = [empty_message]
        footer = [
            "",
            "Comandos: numero=selecionar | texto ou /texto=filtrar | Enter=limpar",
            "          n=proxima | p=anterior | q=voltar/sair",
        ]
        box(title, header + rows + footer)

        choice = ask("> ")
        lowered = choice.casefold()
        if lowered in ("q", "quit", "sair", "voltar"):
            return None
        if lowered in ("n", "next", "proxima"):
            if page + 1 < pages:
                page += 1
            continue
        if lowered in ("p", "prev", "anterior"):
            if page > 0:
                page -= 1
            continue
        if choice == "":
            filtro = ""
            page = 0
            continue
        if choice.startswith("/"):
            filtro = choice[1:].strip()
            page = 0
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(current):
            return current[int(choice) - 1]
        filtro = choice
        page = 0


def subscription_formatter(sub) -> str:
    return f"{fit(sub.display_name or '(sem nome)', 46):<46} [{sub.subscription_id}] ({sub.state})"


def account_matcher(account: Account, filtro: str) -> bool:
    terms = filtro.strip().casefold().split()
    if not terms:
        return True
    haystack = (
        f"{account.name} {account.resource_group} {account.location} "
        f"{account.kind} {account.sku} {account.access_tier or ''}"
    ).casefold()
    return all(term in haystack for term in terms)


def account_formatter(metrics_map: dict[str, Metrics]) -> Callable[[Account], str]:
    def fmt(account: Account) -> str:
        metric = metrics_map.get(account.name)
        size = human_bytes(metric.used_bytes) if metric else "n/d"
        blobs = human_count(metric.blob_count) if metric else "n/d"
        return (
            f"{fit(account.name, 28):<28} "
            f"{fit(account.access_tier or '-', 5):<5} "
            f"{size:>12} {blobs:>10} "
            f"{fit(account.resource_group, 22):<22}"
        )

    return fmt


def choose_subscription(credential) -> Optional[str]:
    clear_screen()
    box("Azure Storage Access Tier Manager - TUI", ["Carregando subscriptions..."])
    client = SubscriptionClient(credential)
    subs = list(client.subscriptions.list())
    subs.sort(key=lambda x: (x.display_name or "").casefold())
    if not subs:
        box(
            "Sem subscriptions",
            [
                "Nenhuma subscription encontrada para este login/tenant.",
                "Confira o tenant ou rode az login --tenant <id-do-tenant>.",
            ],
        )
        pause()
        return None

    chosen = menu(
        "Selecionar subscription",
        subs,
        subscription_formatter,
        lambda sub, filtro: subscription_name_matches_filter(sub.display_name, filtro),
        "(nenhuma subscription corresponde ao filtro)",
    )
    return chosen.subscription_id if chosen else None


def load_accounts(credential, subscription_id: str, no_metrics: bool) -> tuple[list[Account], dict]:
    clear_screen()
    box("Storage accounts", ["Listando storage accounts..."])
    storage = StorageManagementClient(credential, subscription_id)
    accounts = list_accounts(storage, subscription_id)
    metrics_map: dict[str, Metrics] = {}
    if accounts and not no_metrics:
        box("Metricas", [f"Coletando tamanho/blobs de {len(accounts)} conta(s)..."])
        monitor = MonitorManagementClient(credential, subscription_id)
        metrics_map = fetch_all_metrics(monitor, accounts)
    return accounts, metrics_map


def detail_lines(account: Account, metric: Optional[Metrics], include_archive: bool) -> list[str]:
    lines = [
        f"Nome           : {account.name}",
        f"Resource group : {account.resource_group}",
        f"Local          : {account.location}",
        f"Kind / SKU     : {account.kind} / {account.sku}",
        f"Tier atual     : {account.access_tier or '(nao suporta access tier)'}",
    ]
    if metric:
        lines.extend(
            [
                f"Tamanho usado  : {human_bytes(metric.used_bytes)}"
                f"  (blobs: {human_bytes(metric.blob_bytes)})",
                f"Numero blobs   : {human_count(metric.blob_count)}"
                f"  (containers: {human_count(metric.container_count)})",
            ]
        )
        if metric.error:
            lines.append(f"Metricas       : parciais ({metric.error})")
    targets = possible_targets(account, include_archive=include_archive)
    if targets:
        lines.append("")
        lines.append("Destinos possiveis:")
        for idx, target in enumerate(targets, 1):
            mark = " [somente blobs]" if target == "Archive" else ""
            lines.append(f"  {idx}) {target}{mark} - {TIER_NOTAS.get(target, '')}")
    else:
        lines.append("")
        lines.append("Destinos possiveis: nenhum para este tipo de conta.")
    return lines


def dry_run_message(account: Account, target: str) -> None:
    lines = [
        "SIMULACAO - nada foi alterado.",
        "",
        f"Conta: {account.name}",
        f"Tier padrao da conta: {account.access_tier or '(n/d)'} -> {target}",
        f"Blobs block existentes seriam movidos para: {target}",
        "",
        "Para aplicar de verdade, use o comando aN na tela de detalhe",
        "ex.: a1 para aplicar o destino 1.",
    ]
    clear_screen()
    box("Dry-run", lines)
    pause()


def apply_change(credential, account: Account, target: str, workers: int) -> None:
    clear_screen()
    box(
        "Confirmacao",
        [
            f"Conta : {account.name}",
            f"Destino: {target}",
            "",
            "Isto pode gerar custos e alterar o tier da conta/blobs.",
            "Digite ALTERAR para confirmar.",
        ],
    )
    if ask("> ") != "ALTERAR":
        box("Cancelado", ["Nada foi alterado."])
        pause()
        return

    storage = StorageManagementClient(credential, account.subscription_id)
    update_account_tier(storage, account, target, dry_run=False)

    try:
        svc = make_blob_service(account, credential, storage)
    except Exception:
        box("Erro", ["Tier da conta tratado, mas nao foi possivel acessar os blobs."])
        pause()
        return

    items, already, skipped = collect_blobs(svc, target)
    box(
        "Blobs",
        [
            f"A alterar         : {len(items)}",
            f"Ja em {target:<9}: {already}",
            f"Ignorados         : {skipped}",
        ],
    )
    if items and not confirm(f"Alterar {len(items)} blob(s) agora"):
        box("Parcial", ["Tier da conta tratado; blobs nao foram modificados."])
        pause()
        return

    failures = apply_blob_tier(svc, items, target, workers=workers, dry_run=False)
    box(
        "Concluido",
        [
            f"Blobs processados: {len(items)}",
            f"Sucesso          : {len(items) - len(failures)}",
            f"Falhas           : {len(failures)}",
        ],
    )
    pause()


def account_detail(
    credential,
    account: Account,
    metric: Optional[Metrics],
    include_archive: bool,
    workers: int,
) -> None:
    targets = possible_targets(account, include_archive=include_archive)
    while True:
        clear_screen()
        lines = detail_lines(account, metric, include_archive)
        lines.extend(
            [
                "",
                "Comandos: numero=dry-run | aN=aplicar destino N | b=voltar | q=sair",
                "Exemplo : 1 simula o destino 1; a1 aplica de verdade.",
            ]
        )
        box("Detalhe da storage account", lines)

        choice = ask("> ").casefold()
        if choice in ("b", "back", "voltar", ""):
            return
        if choice in ("q", "quit", "sair"):
            raise SystemExit(0)
        if choice.isdigit() and 1 <= int(choice) <= len(targets):
            dry_run_message(account, targets[int(choice) - 1])
            continue
        if choice.startswith("a") and choice[1:].isdigit():
            idx = int(choice[1:])
            if 1 <= idx <= len(targets):
                apply_change(credential, account, targets[idx - 1], workers)
                return


def accounts_loop(
    credential,
    subscription_id: str,
    accounts: list[Account],
    metrics_map: dict[str, Metrics],
    include_archive: bool,
    workers: int,
) -> None:
    if not accounts:
        clear_screen()
        box("Storage accounts", ["Nenhuma storage account encontrada nesta subscription."])
        pause()
        return

    while True:
        chosen = menu(
            "Storage accounts",
            accounts,
            account_formatter(metrics_map),
            account_matcher,
            "(nenhuma storage account corresponde ao filtro)",
        )
        if chosen is None:
            return
        account_detail(
            credential,
            chosen,
            metrics_map.get(chosen.name),
            include_archive,
            workers,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interface ASCII/TUI para gerenciar access tier no terminal."
    )
    parser.add_argument("--tenant", help="Tenant ID (default: sessao atual do az).")
    parser.add_argument("--subscription", help="Subscription ID (pula selecao interativa).")
    parser.add_argument("--no-metrics", action="store_true", help="Nao coleta metricas.")
    parser.add_argument("--include-archive", action="store_true", help="Inclui Archive.")
    parser.add_argument("--workers", type=int, default=16, help="Threads para alterar blobs.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    credential = build_credential(args.tenant)
    subscription_id = args.subscription or choose_subscription(credential)
    if not subscription_id:
        return
    accounts, metrics_map = load_accounts(credential, subscription_id, args.no_metrics)
    accounts_loop(
        credential,
        subscription_id,
        accounts,
        metrics_map,
        include_archive=args.include_archive,
        workers=args.workers,
    )


if __name__ == "__main__":
    try:
        main()
    except (ClientAuthenticationError, HttpResponseError) as exc:
        print(friendly_azure_error(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        sys.exit(130)
