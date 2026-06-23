# 🗃️ Azure Blob Tier Manager

[![ORCID](https://img.shields.io/badge/ORCID-0009--0006--0765--4201-A6CE39?logo=orcid&logoColor=white)](https://orcid.org/0009-0006-0765-4201)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Azure](https://img.shields.io/badge/Cloud-Azure-0078D4?logo=microsoftazure&logoColor=white)](#)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](#)
[![GUI](https://img.shields.io/badge/GUI-tkinter-FFD43B?logo=python&logoColor=black)](#)
[![Last commit](https://img.shields.io/github/last-commit/EdneiMonteiro/azure-blob-tier-manager)](https://github.com/EdneiMonteiro/azure-blob-tier-manager/commits)

Ferramentas (**GUI** + **CLI**) para **visualizar** (e, com permissão, **alterar**) o
*Access Tier* de Storage Accounts do Azure e dos blobs dentro delas. Mostram o tier
atual, o **tamanho usado** e o **número de blobs** por conta, e indicam para quais
tiers cada conta pode mudar (Cool / Cold / Archive).

> ⚠️ Este repositório é uma **demo / utilitário** fornecido "no estado em que se
> encontra". Antes de usar em produção, revise: segurança, permissões, custos e
> impacto. Veja [DISCLAIMER.md](./DISCLAIMER.md) e [SUPPORT.md](./SUPPORT.md).
> **Não afiliado nem endossado pela Microsoft.**

Há duas formas de uso:

| Script | Tipo | Para quê |
|---|---|---|
| `storage_tier_gui.py` | **Janela gráfica** (tkinter) | Navegar visualmente: dropdown de subscription, tabela de contas, menu de contexto para mudar o tier. |
| `storage_tier_manager.py` | **Linha de comando** | Mesmo resultado no terminal, com modo `--show` (somente leitura) e modo de alteração com confirmação. |

> **Somente leitura por padrão.** Visualizar tamanho/blobs usa apenas APIs de
> gestão (ARM) + métricas do Azure Monitor, que exigem só a role **Reader**.
> Mudar o tier de verdade exige permissão de escrita/dados (ex.: *Storage Blob
> Data Contributor* e/ou *Contributor*). Sem ela, a tentativa retorna um erro
> de autorização **sem alterar nada**.

---

## 1. Pré-requisitos

- **Windows** com **Python 3.9+** (testado em 3.13).
  - O módulo `tkinter` (usado pela janela) já vem na instalação padrão do
    Python no Windows — não precisa instalar nada além do `requirements.txt`.
- **Azure CLI** (`az`) instalada e com login feito no tenant desejado.
- Pelo menos a role **Reader** nas subscriptions/contas que você quer ver.

Verifique o Python:

```powershell
python --version
```

---

## 2. Instalação

No diretório do projeto (onde estão os `.py`):

```powershell
cd azure-blob-tier-manager

# (Opcional, recomendado) criar um ambiente virtual isolado
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Instalar as dependências
pip install -r requirements.txt
```

Dependências instaladas (ver `requirements.txt`):

- `azure-identity` — autenticação (usa a sessão do `az login`)
- `azure-mgmt-subscription` — listar subscriptions
- `azure-mgmt-storage` — listar/alterar storage accounts
- `azure-mgmt-monitor` — métricas (tamanho usado, nº de blobs)
- `azure-storage-blob` — alterar o tier dos blobs (plano de dados)

> `tkinter` **não** está no `requirements.txt` porque faz parte da biblioteca
> padrão do Python. Se por acaso der `ModuleNotFoundError: tkinter`, reinstale o
> Python marcando a opção *tcl/tk and IDLE*.

---

## 3. Login no Azure

Faça login no tenant onde estão as storages (ex.: o tenant da CAIXA):

```powershell
az login --tenant <id-do-tenant>
```

Para conferir em qual conta/tenant você está:

```powershell
az account show -o table
```

Os scripts reutilizam essa sessão automaticamente (via `AzureCliCredential`).
Você também pode forçar um tenant com a flag `--tenant <id>` em ambos os scripts.

### 3.1. Rodando no Azure Cloud Shell (terminal, sem login)

No [Azure Cloud Shell](https://shell.azure.com) você **já está autenticado** — não
precisa de `az login`. Porém o Cloud Shell é **só terminal, sem interface gráfica**,
então use apenas a **CLI** (`storage_tier_manager.py`); a janela
(`gui.bat` / `storage_tier_gui.py`) **não funciona** lá (sem display).

```bash
# 1) Obtenha o código (repo privado): clone com gh/git ou faça upload do .py
gh repo clone EdneiMonteiro/azure-blob-tier-manager
cd azure-blob-tier-manager

# 2) Instale as dependências no seu $HOME (persiste no clouddrive)
pip install --user -r requirements.txt

# 3) Rode — já usa a sessão autenticada do Cloud Shell
python storage_tier_manager.py --show
```

Notas:

- O Cloud Shell já traz **Python 3** e o **`az` autenticado**; o script reaproveita
  essa sessão via `AzureCliCredential` (sem `az login`).
- Só `$HOME` / `clouddrive` **persiste** entre sessões; o resto é efêmero.
- Se você tentar abrir a **GUI** sem display, o script detecta e sugere a CLI.

---

## 4. Usando a janela gráfica (recomendado)

```powershell
# Abre a janela (sem console). Use python se quiser ver mensagens no terminal.
pythonw storage_tier_gui.py

# Opcional: forçar um tenant específico
pythonw storage_tier_gui.py --tenant <id-do-tenant>
```

Fluxo na janela:

1. **Filtro** (canto superior esquerdo) — digite parte do nome para achar a
   subscription rapidamente (o tenant pode ter centenas).
2. **Subscription** — selecione no dropdown.
3. **Carregar storages** — preenche a tabela com:
   `Nome · Tier · Tamanho usado · Blobs · Containers · Resource group`.
4. **Clique numa linha** — o painel inferior mostra o tier atual, o uso e para
   quais tiers a conta poderia mudar.
5. **Clique nos cabeçalhos** das colunas para ordenar (por tamanho, blobs etc.).
6. **Botão direito numa conta** → menu **"Mudar tier do blob para ▸"**
   (Hot / Cool / Cold / Archive):
   - Abre um diálogo com resumo + um checkbox **"Apenas simular (dry-run)"**,
     **ligado por padrão**.
   - **Confirmar** com o dry-run ligado: apenas mostra o que faria (não altera nada).
   - Desmarque o dry-run para **tentar de verdade**. Sem permissão de escrita,
     aparece um erro de autorização — sem nenhum blob ser tocado.

---

## 5. Usando a linha de comando

### 5.1. Somente leitura (interativo)

```powershell
python storage_tier_manager.py --show
```

- Lista as subscriptions (com filtro: digite `/texto` para filtrar, `Enter`
  limpa o filtro).
- Mostra a tabela de storages com **tamanho** e **nº de blobs**.
- Digite o número de uma conta para ver o tier atual e as opções de mudança.
  `a` mostra todas; `q` sai.

Pular a coleta de métricas (mais rápido):

```powershell
python storage_tier_manager.py --show --no-metrics
```

Ver uma subscription/conta específica sem interação:

```powershell
python storage_tier_manager.py --subscription <id> --show
python storage_tier_manager.py --subscription <id> --account <nome-da-conta> --show
```

### 5.2. Alterar o tier (precisa de permissão)

> ⚠️ Isto **altera de verdade** o tier padrão da conta e o tier dos blobs block
> existentes. Comece sempre com `--dry-run`.

```powershell
# Simular (não altera nada)
python storage_tier_manager.py --subscription <id> --account <nome> --tier Cool --dry-run

# Aplicar (pede confirmação)
python storage_tier_manager.py --subscription <id> --account <nome> --tier Cool

# Aplicar sem perguntar (automação)
python storage_tier_manager.py --subscription <id> --account <nome> --tier Cool --yes
```

### 5.3. Todas as flags

| Flag | Descrição |
|---|---|
| `--tenant <id>` | Tenant a usar (default: sessão atual do `az`). |
| `--subscription <id>` | Subscription (pula a seleção interativa). |
| `--account <nome>` | Storage account (pula a seleção interativa). |
| `--tier <Hot\|Cool\|Cold\|Archive>` | Tier de destino. |
| `--show` | **Somente leitura**: lista tier, tamanho e destinos. Não altera nada. |
| `--no-metrics` | No `--show`, não consulta tamanho/nº de blobs (mais rápido). |
| `--include-archive` | Inclui Archive no menu de alteração. |
| `--workers <n>` | Threads para alterar blobs (default 16). |
| `--dry-run` | Simula a alteração, sem aplicar. |
| `--yes` | Não pede confirmação. |

Ajuda:

```powershell
python storage_tier_manager.py --help
```

---

## 6. Sobre os tiers

| Tier | Uso típico | Observações |
|---|---|---|
| **Hot** | Acesso frequente | Armazenamento mais caro, acesso barato. |
| **Cool** | Acesso pouco frequente | Permanência mínima ~30 dias; penalidade de retirada antecipada. |
| **Cold** | Raramente acessado | Permanência mínima ~90 dias; mais barato para guardar. |
| **Archive** | Arquivamento | **Só no nível do blob** (não é tier de conta). Blobs ficam offline; ler exige **reidratação** (horas). |

Mudar de tier pode gerar **custos de retirada/exclusão antecipada**. Avalie antes.

---

## 7. Como funciona (resumo técnico)

- **Autenticação:** `AzureCliCredential` reutiliza o `az login` (fallback para
  `DefaultAzureCredential`).
- **Listagem:** `azure-mgmt-subscription` e `azure-mgmt-storage` (plano de gestão).
- **Tamanho / nº de blobs:** métricas do **Azure Monitor**
  (`UsedCapacity`, `BlobCapacity`, `BlobCount`, `ContainerCount`) — coletadas em
  paralelo, exigem só **Reader**. Não enumeram blobs (não usam o plano de dados).
- **Alteração do tier da conta:** `storage_accounts.update` (plano de gestão).
- **Alteração do tier dos blobs:** `set_standard_blob_tier` via
  `azure-storage-blob` (plano de dados); tenta chave de conta e cai para Azure AD.
- **GUI:** `tkinter`; chamadas de rede rodam em threads e entregam o resultado à
  interface por uma fila (a thread da UI nunca é bloqueada).

---

## 8. Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| `AuthorizationFailed` ao mudar tier | Você só tem **Reader** | Esperado para visualização. Para alterar, peça *Contributor* / *Storage Blob Data Contributor*. |
| `Tamanho`/`Blobs` aparecem como `n/d` | Métrica sem ponto recente (conta vazia ou só com page blobs) | Normal. As métricas de capacidade são snapshots ~diários. |
| Nenhuma subscription listada | `az login` em outro tenant | Rode `az login --tenant <id>` correto. |
| `ModuleNotFoundError: tkinter` | Python sem tcl/tk | Reinstale o Python marcando *tcl/tk and IDLE*. |
| Janela não abre com `pythonw` | Erro silencioso (sem console) | Rode com `python storage_tier_gui.py` para ver a mensagem de erro. |
| Acentos estranhos no terminal | Console em cp1252 | Apenas cosmético; os arquivos e a janela usam UTF-8. |

---

## 9. Arquivos do projeto

```
storage_tier_gui.py       # Janela gráfica (tkinter) — visual + menu de contexto
storage_tier_manager.py   # Linha de comando — modo --show e modo de alteração
requirements.txt          # Dependências (pip install -r requirements.txt)
gui.bat                   # Launcher da janela (1 clique)
README.md                 # Este arquivo
CITATION.cff              # Como citar este projeto
DISCLAIMER.md             # Aviso legal
SUPPORT.md                # Política de suporte
LICENSE                   # Licença MIT
```

---

## 10. Citação

Se este material for útil, por favor cite (há um botão **"Cite this repository"**
na página do GitHub, gerado a partir do [`CITATION.cff`](./CITATION.cff)):

> Monteiro, E. *Azure Blob Tier Manager — GUI e CLI para Access Tier de Storage
> Accounts e Blobs do Azure.* https://github.com/EdneiMonteiro/azure-blob-tier-manager

[![ORCID](https://img.shields.io/badge/ORCID-0009--0006--0765--4201-A6CE39?logo=orcid&logoColor=white)](https://orcid.org/0009-0006-0765-4201)

---

## 11. Suporte e Aviso Legal

- Sem SLA nem suporte oficial. Veja [SUPPORT.md](./SUPPORT.md).
- Uso sujeito ao [DISCLAIMER.md](./DISCLAIMER.md).
- **Não afiliado nem endossado pela Microsoft.** Marcas usadas apenas para descrição.

---

## 12. Licença

Distribuído sob a licença **MIT**. Veja [LICENSE](./LICENSE).
