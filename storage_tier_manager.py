#!/usr/bin/env python3
"""
storage_tier_manager.py
=======================

Script interativo para gerenciar o *Access Tier* (camada de acesso) de Storage
Accounts do Azure e dos blobs contidos nelas.

Fluxo:
  1. Autentica no tenant informado (usa a sessão do `az login`).
  2. Lista as subscriptions disponíveis -> você escolhe uma.
  3. Lista as Storage Accounts da subscription -> você escolhe uma.
  4. Mostra o Access Tier atual (ex.: Hot) e as opções para onde pode mudar
     (Cool, Cold, Archive).
  5. Pede confirmação.
  6. Altera o tier *default* da conta e o tier de cada blob block existente,
     exibindo uma tabela de status (path atual, processados/total e ETA).

Uso típico (tenant CAIXA):
    az login --tenant <id-do-tenant-caixa>
    python storage_tier_manager.py --tenant <id-do-tenant-caixa>

Flags úteis:
    --tenant <id>          Tenant a usar (default: assinatura atual do az).
    --subscription <id>    Pula a seleção interativa de subscription.
    --account <nome>       Pula a seleção interativa da storage account.
    --tier <Hot|Cool|Cold|Archive>   Tier de destino (pula o menu).
    --include-archive      Habilita Archive como opção no menu.
    --workers <n>          Threads para alterar blobs (default: 16).
    --dry-run              Simula: não altera nada, só mostra o que faria.
    --yes                  Não pede confirmação (modo não interativo).
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Imports do SDK do Azure (com mensagem amigável se faltar pacote)
# ---------------------------------------------------------------------------
try:
    from azure.identity import AzureCliCredential, DefaultAzureCredential
    from azure.mgmt.subscription import SubscriptionClient
    from azure.mgmt.storage import StorageManagementClient
    from azure.mgmt.monitor import MonitorManagementClient
    from azure.mgmt.storage.models import StorageAccountUpdateParameters
    from azure.storage.blob import BlobServiceClient
    from azure.core.exceptions import (
        ClientAuthenticationError,
        HttpResponseError,
        ResourceNotFoundError,
    )
except ImportError as exc:  # pragma: no cover
    print(
        "Faltam pacotes do Azure. Instale com:\n"
        "    pip install azure-identity azure-mgmt-subscription "
        "azure-mgmt-storage azure-mgmt-monitor azure-storage-blob\n"
        f"\nDetalhe: {exc}"
    )
    sys.exit(1)


logging.getLogger("azure.mgmt.storage._utils.model_base").setLevel(logging.ERROR)

# Tiers válidos no nível da CONTA (default access tier).
ACCOUNT_TIERS = ["Hot", "Cool", "Cold"]
# Tiers válidos no nível do BLOB (Archive só existe aqui).
BLOB_TIERS = ["Hot", "Cool", "Cold", "Archive"]


def enum_str(value) -> Optional[str]:
    """Normaliza enums do SDK (ex.: AccessTier.HOT) para seu valor ('Hot').

    Os enums do SDK herdam de str, então str() devolve 'AccessTier.HOT'.
    Aqui usamos sempre o .value quando existir; strings simples passam direto.
    """
    if value is None:
        return None
    return str(getattr(value, "value", value))


# ===========================================================================
# Utilidades de console
# ===========================================================================
def hr(char: str = "-", width: int = 70) -> str:
    return char * width


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelado.")
        sys.exit(130)


def confirm(prompt: str) -> bool:
    return ask(f"{prompt} [s/N]: ").lower() in ("s", "sim", "y", "yes")


def fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN
        return "--:--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def human_bytes(num: Optional[float]) -> str:
    """Formata bytes em unidade legível (KiB, MiB, GiB...). None -> 'n/d'."""
    if num is None:
        return "n/d"
    num = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(num) < 1024.0:
            return f"{num:0.0f} {unit}" if unit == "B" else f"{num:0.2f} {unit}"
        num /= 1024.0
    return f"{num:0.2f} EiB"


def human_count(num: Optional[float]) -> str:
    """Formata uma contagem inteira; None -> 'n/d'."""
    return "n/d" if num is None else f"{int(round(num)):,}".replace(",", ".")


def friendly_azure_error(exc: Exception) -> str:
    """Formata um erro do Azure (HttpResponseError/Auth) de forma legível, sem traceback."""
    code = None
    try:
        code = getattr(getattr(exc, "error", None), "code", None)
    except Exception:
        code = None
    first = (str(exc).splitlines() or [exc.__class__.__name__])[0]

    lines = ["", hr("="), "ERRO DE ACESSO AO AZURE", hr("=")]
    if code:
        lines.append(f"  Código : {code}")
    lines.append(f"  Detalhe: {first}")
    if (code == "AuthorizationFailed") or ("Authorization" in first) or ("not authorized" in first):
        lines += [
            "",
            "  Você provavelmente não tem a role necessária neste escopo:",
            "   • Para VISUALIZAR: peça 'Reader' na subscription/conta.",
            "   • Para ALTERAR tier: 'Contributor' e/ou 'Storage Blob Data Contributor'.",
            "   • Confirme tenant/subscription: az account show -o table",
            "   • Se usa PIM/JIT, verifique se a elevação ainda está ativa.",
        ]
    elif code in ("InvalidAuthenticationTokenTenant", "InvalidAuthenticationToken"):
        lines += [
            "",
            "  Token de outro tenant. Faça login no tenant correto:",
            "   • az login --tenant <id-do-tenant>",
        ]
    elif is_too_many_requests(exc):
        lines += [
            "",
            "  O Azure retornou throttling (429/Too Many Requests).",
            "  Tente novamente em alguns minutos ou reduza operações simultâneas.",
        ]
    lines.append(hr("="))
    return "\n".join(lines)


def is_too_many_requests(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    return str(status_code) == "429" or "Too Many Requests" in str(exc)


def retry_after_seconds(exc: Exception, fallback: float) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return max(float(retry_after), 0) if retry_after else fallback
    except (TypeError, ValueError):
        return fallback


def run_azure_with_retry(description: str, operation, retries: int = 5):
    """Executa uma chamada ARM com retry simples para throttling 429."""
    for attempt in range(retries + 1):
        try:
            return operation()
        except HttpResponseError as exc:
            if not is_too_many_requests(exc) or attempt >= retries:
                raise
            fallback = min(5 * (2 ** attempt), 60)
            delay = retry_after_seconds(exc, fallback)
            print(
                f"\n[Azure] {description}: Too Many Requests. "
                f"Aguardando {delay:0.0f}s antes de tentar novamente "
                f"({attempt + 1}/{retries})..."
            )
            time.sleep(delay)


# ===========================================================================
# Progresso com tabela de status (path atual | processados/total | ETA)
# ===========================================================================
class Progress:
    """Reporta progresso de forma thread-safe, em janelas de tempo."""

    def __init__(self, total: int, every_seconds: float = 3.0):
        self.total = total
        self.every = every_seconds
        self.done = 0
        self.errors = 0
        self.current = ""
        self.start = time.monotonic()
        self._last_print = 0.0
        self._lock = threading.Lock()

    def update(self, path: str, ok: bool) -> None:
        with self._lock:
            self.done += 1
            if not ok:
                self.errors += 1
            self.current = path
            now = time.monotonic()
            if now - self._last_print >= self.every or self.done == self.total:
                self._last_print = now
                self._print_locked()

    def _print_locked(self) -> None:
        elapsed = time.monotonic() - self.start
        rate = self.done / elapsed if elapsed > 0 else 0
        remaining = self.total - self.done
        eta = remaining / rate if rate > 0 else float("nan")
        pct = (self.done / self.total * 100) if self.total else 100.0
        path = self.current if len(self.current) <= 56 else "..." + self.current[-53:]
        print(
            "\n+-- status " + hr("-", 58) + "\n"
            f"| atual    : {path}\n"
            f"| progresso: {self.done}/{self.total} ({pct:0.1f}%)  erros: {self.errors}\n"
            f"| ETA      : {fmt_eta(eta)}\n"
            "+" + hr("-", 67)
        )


# ===========================================================================
# Modelo simples para a Storage Account selecionada
# ===========================================================================
@dataclass
class Account:
    name: str
    resource_group: str
    location: str
    kind: str
    sku: str
    access_tier: Optional[str]
    allow_shared_key: Optional[bool]
    blob_endpoint: str
    subscription_id: str
    resource_id: str = ""


def parse_rg(resource_id: str) -> str:
    parts = resource_id.split("/")
    for i, p in enumerate(parts):
        if p.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


# ===========================================================================
# Métricas de capacidade (Azure Monitor) — só precisa de role 'Reader'
# ===========================================================================
BLOB_SVC_NS = "Microsoft.Storage/storageAccounts/blobServices"


@dataclass
class Metrics:
    used_bytes: Optional[float] = None      # UsedCapacity (conta inteira)
    blob_bytes: Optional[float] = None      # BlobCapacity (serviço de blob)
    blob_count: Optional[float] = None      # BlobCount
    container_count: Optional[float] = None  # ContainerCount
    error: Optional[str] = None


def _last_average(monitor_client, resource_uri, metric_names, timespan, namespace=None):
    """Retorna {metric_name: ultimo_valor_medio} usando Azure Monitor.

    Métricas de capacidade são emitidas ~1x/dia; pegamos o ponto mais recente
    não-nulo dentro do timespan.
    """
    kw = dict(
        resource_uri=resource_uri,
        metricnames=",".join(metric_names),
        timespan=timespan,
        interval="PT1H",
        aggregation="Average",
    )
    if namespace:
        kw["metricnamespace"] = namespace
    res = monitor_client.metrics.list(**kw)
    out = {}
    for m in res.value:
        pts = [d.average for t in m.timeseries for d in t.data if d.average is not None]
        out[m.name.value if hasattr(m.name, "value") else str(m.name)] = (
            pts[-1] if pts else None
        )
    return out


def fetch_metrics(monitor_client, account: Account) -> Metrics:
    """Busca capacidade usada e nº de blobs/containers de uma conta (read-only)."""
    if not account.resource_id:
        return Metrics(error="sem resource id")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=2)
    timespan = start.strftime("%Y-%m-%dT%H:%M:%SZ") + "/" + end.strftime("%Y-%m-%dT%H:%M:%SZ")
    m = Metrics()
    try:
        acct = _last_average(monitor_client, account.resource_id, ["UsedCapacity"], timespan)
        m.used_bytes = acct.get("UsedCapacity")
    except Exception as exc:  # noqa: BLE001
        m.error = str(exc).splitlines()[0][:80]
    try:
        bs = _last_average(
            monitor_client,
            account.resource_id + "/blobServices/default",
            ["BlobCapacity", "BlobCount", "ContainerCount"],
            timespan,
            namespace=BLOB_SVC_NS,
        )
        m.blob_bytes = bs.get("BlobCapacity")
        m.blob_count = bs.get("BlobCount")
        m.container_count = bs.get("ContainerCount")
    except Exception as exc:  # noqa: BLE001
        if not m.error:
            m.error = str(exc).splitlines()[0][:80]
    return m


def fetch_all_metrics(monitor_client, accounts: list[Account], workers: int = 8) -> dict:
    """Busca métricas de todas as contas em paralelo. Retorna {nome: Metrics}."""
    result: dict[str, Metrics] = {}
    if not accounts:
        return result
    print(f"\nColetando capacidade/nº de blobs de {len(accounts)} conta(s) via Azure Monitor...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_metrics, monitor_client, a): a for a in accounts}
        for fut in as_completed(futs):
            a = futs[fut]
            try:
                result[a.name] = fut.result()
            except Exception as exc:  # noqa: BLE001
                result[a.name] = Metrics(error=str(exc)[:80])
    return result


# ===========================================================================
# Autenticação
# ===========================================================================
def build_credential(tenant_id: Optional[str]):
    """Usa a sessão do `az login`; cai para DefaultAzureCredential se preciso."""
    try:
        if tenant_id:
            return AzureCliCredential(tenant_id=tenant_id)
        return AzureCliCredential()
    except Exception:
        if tenant_id:
            return DefaultAzureCredential(
                interactive_browser_tenant_id=tenant_id,
                shared_cache_tenant_id=tenant_id,
            )
        return DefaultAzureCredential()


# ===========================================================================
# Seleção de subscription
# ===========================================================================
def subscription_name_matches_filter(
    display_name: Optional[str],
    filter_text: str,
) -> bool:
    """Filtra subscription por nome, aceitando múltiplos termos."""
    terms = filter_text.strip().casefold().split()
    if not terms:
        return True
    haystack = (display_name or "").casefold()
    return all(term in haystack for term in terms)


def pick_subscription(credential, preset: Optional[str]) -> str:
    if preset:
        return preset
    sub_client = SubscriptionClient(credential)
    # O escopo de tenant é definido pela credencial (AzureCliCredential(tenant_id=...)),
    # então a listagem já retorna apenas as subscriptions do tenant autenticado.
    subs = list(sub_client.subscriptions.list())

    if not subs:
        print(
            "Nenhuma subscription encontrada para este login/tenant.\n"
            "Rode: az login --tenant <id-do-tenant>"
        )
        sys.exit(1)

    subs.sort(key=lambda x: (x.display_name or "").lower())

    # Como o tenant pode ter centenas de subscriptions, permitimos filtrar por
    # parte do nome antes de escolher pelo número.
    cap = 40  # máximo de linhas exibidas por vez
    filtro = ""
    while True:
        visiveis = [
            s for s in subs
            if subscription_name_matches_filter(s.display_name, filtro)
        ]
        print(f"\nSubscriptions ({len(visiveis)} de {len(subs)})"
              + (f" — filtro: '{filtro}'" if filtro else "") + ":")
        print(hr())
        if not visiveis:
            print("  (nenhuma corresponde ao filtro)")
        for i, s in enumerate(visiveis[:cap], 1):
            print(f"  {i:>3}) {s.display_name}  [{s.subscription_id}]  ({s.state})")
        if len(visiveis) > cap:
            print(f"  ... e mais {len(visiveis) - cap}. Digite texto para filtrar.")
        print(hr())
        print("  Digite o NÚMERO para escolher, texto para filtrar por nome, "
              "'/texto' também funciona, ou Enter para limpar o filtro.")

        choice = ask("Subscription/filtro: ")
        if choice.startswith("/"):
            filtro = choice[1:].strip()
            continue
        if choice == "":
            filtro = ""
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(visiveis):
            chosen = visiveis[int(choice) - 1]
            print(f"-> {chosen.display_name} ({chosen.subscription_id})")
            return chosen.subscription_id
        filtro = choice


# ===========================================================================
# Seleção de storage account
# ===========================================================================
def list_accounts(storage_client, subscription_id) -> list[Account]:
    accounts = []
    for a in storage_client.storage_accounts.list():
        rg = parse_rg(a.id)
        blob_ep = ""
        if a.primary_endpoints and a.primary_endpoints.blob:
            blob_ep = a.primary_endpoints.blob
        else:
            blob_ep = f"https://{a.name}.blob.core.windows.net/"
        accounts.append(
            Account(
                name=a.name,
                resource_group=rg,
                location=a.location,
                kind=enum_str(a.kind) or "",
                sku=enum_str(a.sku.name) if a.sku else "",
                access_tier=enum_str(a.access_tier),
                allow_shared_key=a.allow_shared_key_access,
                blob_endpoint=blob_ep,
                subscription_id=subscription_id,
                resource_id=a.id,
            )
        )
    accounts.sort(key=lambda x: x.name.lower())
    return accounts


def pick_account(accounts: list[Account], preset_name: Optional[str]) -> Account:
    if preset_name:
        for a in accounts:
            if a.name.lower() == preset_name.lower():
                return a
        print(f"Storage account '{preset_name}' não encontrada na subscription.")
        sys.exit(1)

    if not accounts:
        print("Nenhuma storage account encontrada nesta subscription.")
        sys.exit(1)

    print("\nStorage accounts:")
    print(hr())
    print(f"  {'#':>3}  {'NOME':<26} {'RG':<24} {'KIND':<14} {'TIER':<8}")
    print(hr())
    for i, a in enumerate(accounts, 1):
        tier = a.access_tier or "-"
        print(f"  {i:>3}) {a.name:<26.26} {a.resource_group:<24.24} {a.kind:<14.14} {tier:<8}")
    print(hr())

    while True:
        choice = ask("Escolha a storage account (número): ")
        if choice.isdigit() and 1 <= int(choice) <= len(accounts):
            return accounts[int(choice) - 1]
        print("Opção inválida.")


# Notas curtas sobre cada tier de destino (usadas no modo --show e no menu).
TIER_NOTAS = {
    "Hot": "acesso frequente",
    "Cool": "acesso pouco frequente; permanência mínima 30 dias",
    "Cold": "raramente acessado; mínima 90 dias; mais barato p/ guardar",
    "Archive": "offline; só nível de blob; leitura exige reidratação (horas)",
}


def print_account_detail(
    account: Account,
    metrics: Optional[Metrics] = None,
    include_archive: bool = True,
) -> None:
    """Imprime o detalhe SOMENTE LEITURA de uma conta: tier, uso e destinos."""
    atual = account.access_tier or "(não suporta access tier)"
    print(f"\n• {account.name}")
    print(f"    Resource group : {account.resource_group}")
    print(f"    Local          : {account.location}")
    print(f"    Kind / SKU     : {account.kind} / {account.sku}")
    print(f"    Tier ATUAL     : {atual}")
    if metrics is not None:
        print(f"    Tamanho usado  : {human_bytes(metrics.used_bytes)}"
              f"  (blobs: {human_bytes(metrics.blob_bytes)})")
        print(f"    Nº de blobs    : {human_count(metrics.blob_count)}"
              f"  (containers: {human_count(metrics.container_count)})")
        if metrics.error:
            print(f"    (métricas parciais: {metrics.error})")
    if account.access_tier is None:
        print("    Pode mudar p/  : — (este tipo de conta não tem access tier)")
    else:
        print("    Pode mudar p/  :")
        for t in possible_targets(account, include_archive=include_archive):
            marca = " [somente blobs]" if t == "Archive" else ""
            print(f"        - {t}{marca}  ({TIER_NOTAS.get(t, '')})")


def show_accounts(
    accounts: list[Account],
    metrics_map: dict,
    include_archive: bool,
    only_name: Optional[str],
) -> None:
    """Modo SOMENTE LEITURA não interativo: despeja todas as contas (ou uma)."""
    if only_name:
        accounts = [a for a in accounts if a.name.lower() == only_name.lower()]
        if not accounts:
            print(f"Storage account '{only_name}' não encontrada na subscription.")
            sys.exit(1)

    if not accounts:
        print("Nenhuma storage account encontrada nesta subscription.")
        return

    print("\n" + hr("="))
    print(f"STORAGES (somente leitura) — {len(accounts)} conta(s)")
    print(hr("="))
    for a in accounts:
        print_account_detail(a, metrics_map.get(a.name), include_archive=include_archive)
    print("\n" + hr("="))
    print("Nada foi alterado. Para aplicar mudanças é preciso permissão de dados")
    print("('Storage Blob Data Contributor') e/ou de gestão na conta.")
    print(hr("="))


def print_accounts_table(accounts: list[Account], metrics_map: dict) -> None:
    """Imprime a tabela-resumo: nome, tier, tamanho e nº de blobs."""
    print("\n" + hr("=", 84))
    print(f"STORAGE ACCOUNTS (somente leitura) — {len(accounts)} conta(s)")
    print(hr("=", 84))
    print(f"  {'#':>3}  {'NOME':<30} {'TIER':<5} {'TAMANHO':>12} {'BLOBS':>10} {'RG':<18}")
    print(hr("-", 84))
    for i, a in enumerate(accounts, 1):
        tier = a.access_tier or "-"
        m = metrics_map.get(a.name)
        size = human_bytes(m.used_bytes) if m else "n/d"
        blobs = human_count(m.blob_count) if m else "n/d"
        print(f"  {i:>3})  {a.name:<30.30} {tier:<5} {size:>12} {blobs:>10} "
              f"{a.resource_group:<18.18}")
    print(hr("-", 84))


def show_report(accounts: list[Account], metrics_map: dict, include_archive: bool) -> None:
    """Relatório completo NÃO interativo: tabela + detalhe de todas as contas."""
    print_accounts_table(accounts, metrics_map)
    for a in accounts:
        print_account_detail(a, metrics_map.get(a.name), include_archive=include_archive)
    print("\n" + hr("="))
    print("Relatório completo acima. Nada foi alterado (somente leitura).")
    print(hr("="))


def show_interactive(accounts: list[Account], metrics_map: dict, include_archive: bool) -> None:
    """Modo SOMENTE LEITURA interativo: lista storages, você escolhe e vê os detalhes.

    Não acessa blobs e não altera nada. Se a entrada acabar (pipe/sem console),
    cai para o relatório completo e encerra sem erro.
    """
    if not accounts:
        print("Nenhuma storage account encontrada nesta subscription.")
        return

    while True:
        print_accounts_table(accounts, metrics_map)
        print("  Digite o NÚMERO para ver tier atual + opções, "
              "'a' p/ todas, ou 'q' p/ sair.")

        try:
            choice = input("Storage: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Sem entrada interativa: imprime o relatório completo e sai limpo.
            print("\n(Sem entrada interativa — exibindo relatório completo.)")
            show_report(accounts, metrics_map, include_archive)
            return
        if choice in ("q", "sair", "quit", "exit"):
            print("Saindo (nada foi alterado).")
            return
        if choice in ("a", "todas", "all"):
            for a in accounts:
                print_account_detail(a, metrics_map.get(a.name), include_archive=include_archive)
            print("\n(Nada foi alterado — somente leitura.)")
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(accounts):
            a = accounts[int(choice) - 1]
            print_account_detail(a, metrics_map.get(a.name), include_archive=include_archive)
            print("\n(Nada foi alterado — somente leitura.)")
            continue
        print("Opção inválida.")


# ===========================================================================
# Menu de tier de destino
# ===========================================================================
def possible_targets(account: "Account", include_archive: bool = True) -> list[str]:
    """Tiers para onde a conta/blobs poderiam mudar, a partir do tier atual.

    Não acessa nada na nuvem — apenas calcula com base no access_tier atual.
    Retorna lista vazia se a conta não suporta access tier.
    """
    current = account.access_tier
    if current is None:
        return []
    options = [t for t in ACCOUNT_TIERS if t != current]
    if include_archive:
        options.append("Archive")  # Archive existe só no nível do blob
    return options


def pick_target_tier(account: Account, preset: Optional[str], include_archive: bool) -> str:
    current = account.access_tier
    options = [t for t in ACCOUNT_TIERS if t != current]
    if include_archive:
        options.append("Archive")

    if preset:
        preset_norm = preset.capitalize()
        if preset_norm not in BLOB_TIERS:
            print(f"Tier '{preset}' inválido. Use: {', '.join(BLOB_TIERS)}")
            sys.exit(1)
        return preset_norm

    print(f"\nAccess tier atual da conta: {current or '(não suporta access tier)'}")
    print("Para onde deseja mudar?")
    print(hr())
    for i, t in enumerate(options, 1):
        nota = ""
        if t == "Archive":
            nota = "  (apenas blobs; conta segue no tier atual; leitura exige reidratação)"
        elif t == "Cold":
            nota = "  (mais barato p/ armazenar, penalidade de retirada antecipada)"
        elif t == "Cool":
            nota = "  (acesso pouco frequente; min. 30 dias)"
        print(f"  {i}) {t}{nota}")
    print("  0) Cancelar")
    print(hr())

    while True:
        choice = ask("Opção: ")
        if choice == "0":
            print("Cancelado.")
            sys.exit(0)
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        print("Opção inválida.")


# ===========================================================================
# Cliente de dados (blob) — tenta chave de conta; cai para AAD
# ===========================================================================
def make_blob_service(account: Account, credential, storage_client) -> BlobServiceClient:
    # 1) Chave de conta (se shared key não estiver desabilitada).
    if account.allow_shared_key is not False:
        try:
            keys = storage_client.storage_accounts.list_keys(
                account.resource_group, account.name
            )
            key = keys.keys[0].value
            svc = BlobServiceClient(account_url=account.blob_endpoint, credential=key)
            # valida com uma listagem leve
            next(iter(svc.list_containers(results_per_page=1)), None)
            return svc
        except Exception:
            pass  # cai para AAD

    # 2) Azure AD (precisa de role de dados, ex.: Storage Blob Data Contributor).
    svc = BlobServiceClient(account_url=account.blob_endpoint, credential=credential)
    try:
        next(iter(svc.list_containers(results_per_page=1)), None)
    except Exception as exc:
        print(
            "\nNão consegui acessar o plano de DADOS dos blobs.\n"
            "Verifique se você tem a role 'Storage Blob Data Contributor' na conta,\n"
            "ou se o acesso por chave compartilhada está habilitado.\n"
            f"Detalhe: {exc}"
        )
        raise
    return svc


# ===========================================================================
# Coleta de blobs e aplicação do tier
# ===========================================================================
@dataclass
class BlobItem:
    container: str
    name: str
    current_tier: Optional[str]


def collect_blobs(svc: BlobServiceClient, target: str) -> tuple[list[BlobItem], int, int]:
    """Retorna (itens_a_mudar, ja_no_destino, ignorados_nao_block)."""
    to_change: list[BlobItem] = []
    already = 0
    skipped = 0
    print("\nMapeando blobs (pode levar um tempo em contas grandes)...")
    for container in svc.list_containers():
        cclient = svc.get_container_client(container.name)
        for b in cclient.list_blobs():
            if enum_str(b.blob_type) != "BlockBlob":
                skipped += 1
                continue
            cur = enum_str(b.blob_tier)
            if cur == target:
                already += 1
                continue
            to_change.append(BlobItem(container.name, b.name, cur))
    return to_change, already, skipped


def apply_blob_tier(
    svc: BlobServiceClient,
    items: list[BlobItem],
    target: str,
    workers: int,
    dry_run: bool,
) -> list[tuple[str, str]]:
    """Aplica o tier nos blobs em paralelo. Retorna lista de (path, erro)."""
    failures: list[tuple[str, str]] = []
    if not items:
        return failures

    progress = Progress(total=len(items))

    def work(item: BlobItem):
        path = f"{item.container}/{item.name}"
        if dry_run:
            return path, True, ""
        try:
            bclient = svc.get_blob_client(item.container, item.name)
            kwargs = {}
            # Reidratação só se o blob estiver em Archive saindo para tier online.
            if item.current_tier == "Archive" and target != "Archive":
                kwargs["rehydrate_priority"] = "Standard"
            bclient.set_standard_blob_tier(target, **kwargs)
            return path, True, ""
        except Exception as exc:  # noqa: BLE001
            return path, False, str(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, it) for it in items]
        for fut in as_completed(futures):
            path, ok, err = fut.result()
            progress.update(path, ok)
            if not ok:
                failures.append((path, err))

    return failures


# ===========================================================================
# Alteração do tier default da conta
# ===========================================================================
def update_account_tier(storage_client, account: Account, target: str, dry_run: bool) -> bool:
    if account.access_tier is None:
        print(
            f"\n[conta] '{account.name}' não suporta access tier "
            f"(kind={account.kind}/sku={account.sku}). Pulando tier da conta."
        )
        return False
    if target == "Archive":
        print(
            "\n[conta] Archive não é válido como tier default da conta; "
            f"a conta permanece em '{account.access_tier}'. Só os blobs irão para Archive."
        )
        return False
    if account.access_tier == target:
        print(f"\n[conta] já está em '{target}'. Nada a fazer na conta.")
        return False
    if dry_run:
        print(f"\n[conta] (dry-run) mudaria default tier {account.access_tier} -> {target}")
        return True
    run_azure_with_retry(
        f"alterar tier da conta '{account.name}'",
        lambda: storage_client.storage_accounts.update(
            account.resource_group,
            account.name,
            StorageAccountUpdateParameters(access_tier=target),
        ),
    )
    print(f"\n[conta] default tier alterado: {account.access_tier} -> {target}")
    return True


# ===========================================================================
# main
# ===========================================================================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gerencia access tier de storages e blobs no Azure.")
    p.add_argument("--tenant", help="Tenant ID a usar (CAIXA, por exemplo).")
    p.add_argument("--subscription", help="Subscription ID (pula seleção interativa).")
    p.add_argument("--account", help="Nome da storage account (pula seleção interativa).")
    p.add_argument("--tier", help="Tier de destino: Hot, Cool, Cold ou Archive.")
    p.add_argument("--include-archive", action="store_true", help="Mostra Archive no menu.")
    p.add_argument(
        "--show",
        action="store_true",
        help="SOMENTE LEITURA: lista storages, tier atual e destinos possíveis. Não altera nada.",
    )
    p.add_argument(
        "--no-metrics",
        action="store_true",
        help="No modo --show, não consulta tamanho/nº de blobs (Azure Monitor).",
    )
    p.add_argument("--workers", type=int, default=16, help="Threads para alterar blobs.")
    p.add_argument("--dry-run", action="store_true", help="Não altera nada, só simula.")
    p.add_argument("--yes", action="store_true", help="Não pede confirmação.")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Garante acentuação correta em consoles Windows (cp1252 por padrão).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print(hr("="))
    print(" Azure Storage Access Tier Manager")
    if args.show:
        print(" *** MODO SOMENTE LEITURA (--show): nada será alterado ***")
    elif args.dry_run:
        print(" *** MODO DRY-RUN: nada será alterado ***")
    print(hr("="))

    credential = build_credential(args.tenant)

    # 1) Subscription
    subscription_id = pick_subscription(credential, args.subscription)

    # 2) Storage account
    storage_client = StorageManagementClient(credential, subscription_id)
    accounts = list_accounts(storage_client, subscription_id)

    # Modo somente leitura: interativo (lista -> escolhe -> mostra), sem tocar em blobs.
    if args.show:
        metrics_map: dict = {}
        if not args.no_metrics:
            target_accounts = (
                [a for a in accounts if a.name.lower() == args.account.lower()]
                if args.account else accounts
            )
            monitor_client = MonitorManagementClient(credential, subscription_id)
            metrics_map = fetch_all_metrics(monitor_client, target_accounts)
        if args.account:
            show_accounts(accounts, metrics_map, include_archive=True, only_name=args.account)
        else:
            show_interactive(accounts, metrics_map, include_archive=True)
        return

    account = pick_account(accounts, args.account)

    print("\n" + hr())
    print(f"Conta selecionada : {account.name}")
    print(f"Resource group    : {account.resource_group}")
    print(f"Local / Kind / SKU: {account.location} / {account.kind} / {account.sku}")
    print(f"Access tier atual : {account.access_tier or '(não suporta)'}")
    print(hr())

    # 3) Tier de destino
    target = pick_target_tier(account, args.tier, args.include_archive)

    # 4) Resumo + confirmação
    print("\n" + hr("="))
    print("RESUMO DA OPERAÇÃO")
    print(hr("="))
    print(f"  Storage account : {account.name}")
    print(f"  Tier da conta   : {account.access_tier or '(inalterado)'} -> {target}")
    print(f"  Blobs           : serão movidos para '{target}' (block blobs existentes)")
    print("  Atenção         : mudar de tier pode gerar custos de retirada/")
    print("                    exclusão antecipada (Cool/Cold/Archive).")
    if target == "Archive":
        print("  Archive         : blobs ficam offline; leitura exige reidratação (horas).")
    print(hr("="))

    if not args.yes and not args.dry_run:
        if not confirm("Confirmar alteração"):
            print("Cancelado pelo usuário.")
            sys.exit(0)

    # 5) Valida plano de dados antes de alterar a conta, evitando mudança parcial
    # quando falta Storage Blob Data Contributor ou acesso por chave.
    try:
        svc = make_blob_service(account, credential, storage_client)
    except Exception:
        print("Nenhuma alteração aplicada: não foi possível acessar/processar os blobs.")
        sys.exit(1)

    items, already, skipped = collect_blobs(svc, target)
    print(
        f"\nBlobs a alterar: {len(items)} | já em '{target}': {already} | "
        f"ignorados (não-block): {skipped}"
    )

    if items and not args.yes and not args.dry_run:
        if not confirm(f"Alterar {len(items)} blob(s) agora"):
            print("Cancelado pelo usuário. Nada foi alterado.")
            sys.exit(0)

    # 6) Conta
    update_account_tier(storage_client, account, target, args.dry_run)

    # 7) Blobs
    t0 = time.monotonic()
    failures = apply_blob_tier(svc, items, target, args.workers, args.dry_run)
    elapsed = time.monotonic() - t0

    # 8) Relatório final
    print("\n" + hr("="))
    print("CONCLUÍDO")
    print(hr("="))
    print(f"  Blobs processados : {len(items)}")
    print(f"  Sucesso           : {len(items) - len(failures)}")
    print(f"  Falhas            : {len(failures)}")
    print(f"  Tempo total       : {fmt_eta(elapsed)}")
    if failures:
        print("\n  Primeiras falhas:")
        for path, err in failures[:10]:
            print(f"   - {path}: {err[:80]}")
        if len(failures) > 10:
            print(f"   ... e mais {len(failures) - 10}.")
    print(hr("="))


if __name__ == "__main__":
    try:
        main()
    except (ClientAuthenticationError, HttpResponseError) as exc:
        print(friendly_azure_error(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        sys.exit(130)
